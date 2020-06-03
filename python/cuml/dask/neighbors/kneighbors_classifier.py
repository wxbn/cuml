#
# Copyright (c) 2020, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from cuml.dask.common.input_utils import DistributedDataHandler
from cuml.dask.common.input_utils import to_output
from cuml.dask.common import parts_to_ranks
from cuml.dask.common import raise_exception_from_futures
from cuml.dask.common import flatten_grouped_results
from cuml.dask.common.utils import raise_mg_import_exception
from cuml.dask.common.comms import worker_state
from cuml.dask.neighbors import NearestNeighbors
from dask.distributed import wait
import dask.array as da
from uuid import uuid1
import numpy as np


def _custom_getter(o):
    def func_get(f, idx):
        return f[o][idx]
    return func_get


class KNeighborsClassifier(NearestNeighbors):
    """
    Multi-node Multi-GPU K-Nearest Neighbors Classifier Model.

    K-Nearest Neighbors Classifier is an instance-based learning technique,
    that keeps training samples around for prediction, rather than trying
    to learn a generalizable set of model parameters.
    """
    def __init__(self, client=None, streams_per_handle=0,
                 verbose=False, **kwargs):
        super(KNeighborsClassifier, self).__init__(client=client,
                                                   verbose=verbose,
                                                   **kwargs)
        self.streams_per_handle = streams_per_handle

    def fit(self, X, y):
        """
        Fit a multi-node multi-GPU K-Nearest Neighbors Classifier index

        Parameters
        ----------
        X : array-like (device or host) shape = (n_samples, n_features)
            Index data.
            Acceptable formats: dask CuPy/NumPy/Numba Array

        y : array-like (device or host) shape = (n_samples, n_features)
            Index labels data.
            Acceptable formats: dask CuPy/NumPy/Numba Array

        Returns
        -------
        self : KNeighborsClassifier model
        """
        self.data_handler = \
            DistributedDataHandler.create(data=[X, y],
                                          client=self.client)

        uniq_labels = []
        if self.data_handler.datatype == 'cupy':
            if y.ndim == 1:
                uniq_labels.append(da.unique(y))
            else:
                n_targets = y.shape[1]
                for i in range(n_targets):
                    uniq_labels.append(da.unique(y[:, i]))
        else:
            n_targets = y.shape[1]
            for i in range(n_targets):
                uniq_labels.append(y.iloc[:, i].unique())
        self.uniq_labels = np.array(da.compute(uniq_labels)[0])
        self.n_unique = list(map(lambda x: len(x), self.uniq_labels))

        return self

    @staticmethod
    def _func_create_model(sessionId, **kwargs):
        try:
            from cuml.neighbors.kneighbors_classifier_mg import \
                KNeighborsClassifierMG as cumlKNN
        except ImportError:
            raise_mg_import_exception()

        handle = worker_state(sessionId)["handle"]
        return cumlKNN(handle=handle, **kwargs)

    @staticmethod
    def _func_predict(model, data, data_parts_to_ranks, data_nrows,
                      query, query_parts_to_ranks, query_nrows,
                      uniq_labels, n_unique, ncols, rank, convert_dtype,
                      probas_only):
        if probas_only:
            return model.predict_proba(
                data, data_parts_to_ranks, data_nrows,
                query, query_parts_to_ranks, query_nrows,
                uniq_labels, n_unique, ncols, rank, convert_dtype
            )
        else:
            return model.predict(
                data, data_parts_to_ranks, data_nrows,
                query, query_parts_to_ranks, query_nrows,
                uniq_labels, n_unique, ncols, rank, convert_dtype
            )

    def predict(self, X, convert_dtype=True):
        """
        Predict labels for a query from previously stored index
        and index labels.
        The process is done in a multi-node multi-GPU fashion.

        Parameters
        ----------
        X : array-like (device or host) shape = (n_samples, n_features)
            Query data.
            Acceptable formats: dask cuDF, dask CuPy/NumPy/Numba Array

        convert_dtype : bool, optional (default = True)
            When set to True, the predict method will automatically
            convert the data to the right formats.

        Returns
        -------
        predictions : Dask futures or Dask CuPy Arrays
        """
        query_handler = \
            DistributedDataHandler.create(data=X,
                                          client=self.client)
        self.datatype = query_handler.datatype

        comms = KNeighborsClassifier._build_comms(self.data_handler,
                                                  query_handler,
                                                  self.streams_per_handle,
                                                  self.verbose)

        worker_info = comms.worker_info(comms.worker_addresses)

        """
        Build inputs and outputs
        """
        self.data_handler.calculate_parts_to_sizes(comms=comms)
        query_handler.calculate_parts_to_sizes(comms=comms)

        data_parts_to_ranks, data_nrows = \
            parts_to_ranks(self.client,
                           worker_info,
                           self.data_handler.gpu_futures)

        query_parts_to_ranks, query_nrows = \
            parts_to_ranks(self.client,
                           worker_info,
                           query_handler.gpu_futures)

        """
        Each Dask worker creates a single model
        """
        key = uuid1()
        models = dict([(worker, self.client.submit(
            self._func_create_model,
            comms.sessionId,
            **self.kwargs,
            workers=[worker],
            key="%s-%s" % (key, idx)))
            for idx, worker in enumerate(comms.worker_addresses)])

        """
        Invoke knn_classify on Dask workers to perform distributed query
        """
        key = uuid1()
        knn_clf_res = dict([(worker_info[worker]["rank"], self.client.submit(
                            self._func_predict,
                            models[worker],
                            self.data_handler.worker_to_parts[worker] if
                            worker in self.data_handler.workers else [],
                            data_parts_to_ranks,
                            data_nrows,
                            query_handler.worker_to_parts[worker] if
                            worker in query_handler.workers else [],
                            query_parts_to_ranks,
                            query_nrows,
                            self.uniq_labels,
                            self.n_unique,
                            X.shape[1],
                            worker_info[worker]["rank"],
                            convert_dtype,
                            False,
                            key="%s-%s" % (key, idx),
                            workers=[worker]))
                           for idx, worker in enumerate(comms.worker_addresses)
                            ])

        wait(list(knn_clf_res.values()))
        raise_exception_from_futures(list(knn_clf_res.values()))

        """
        Gather resulting partitions and return result
        """
        out_futures = flatten_grouped_results(self.client,
                                              query_parts_to_ranks,
                                              knn_clf_res,
                                              getter_func=_custom_getter(0))

        out_i_futures = flatten_grouped_results(self.client,
                                                query_parts_to_ranks,
                                                knn_clf_res,
                                                getter_func=_custom_getter(1))

        out_d_futures = flatten_grouped_results(self.client,
                                                query_parts_to_ranks,
                                                knn_clf_res,
                                                getter_func=_custom_getter(2))

        comms.destroy()

        out = to_output(out_futures, self.datatype)
        out_i = to_output(out_i_futures, self.datatype)
        out_d = to_output(out_d_futures, self.datatype)
        return out, out_i, out_d

    def score(self, X, y):
        """
        Predict labels for a query from previously stored index
        and index labels.
        The process is done in a multi-node multi-GPU fashion.

        Parameters
        ----------
        X : array-like (device or host) shape = (n_samples, n_features)
            Query test data.
            Acceptable formats: dask CuPy/NumPy/Numba Array

        y : array-like (device or host) shape = (n_samples, n_features)
            Labels test data.
            Acceptable formats: dask CuPy/NumPy/Numba Array

        Returns
        -------
        score
        """
        labels, _, _ = self.predict(X, convert_dtype=True)
        diff = (labels == y)
        if self.data_handler.datatype == 'cupy':
            mean = da.mean(diff)
            return mean.compute()
        else:
            raise ValueError("Only Dask arrays are supported")

    def predict_proba(self, X, convert_dtype=True):
        """
        Predict labels probabilities for a query from
        previously stored index and index labels.
        The process is done in a multi-node multi-GPU fashion.

        Parameters
        ----------
        X : array-like (device or host) shape = (n_samples, n_features)
            Query data.
            Acceptable formats: dask cuDF, dask CuPy/NumPy/Numba Array

        convert_dtype : bool, optional (default = True)
            When set to True, the predict method will automatically
            convert the data to the right formats.

        Returns
        -------
        probabilities : Dask futures or Dask CuPy Arrays
        """
        query_handler = \
            DistributedDataHandler.create(data=X,
                                          client=self.client)
        self.datatype = query_handler.datatype

        comms = KNeighborsClassifier._build_comms(self.data_handler,
                                                  query_handler,
                                                  self.streams_per_handle,
                                                  self.verbose)

        worker_info = comms.worker_info(comms.worker_addresses)

        """
        Build inputs and outputs
        """
        self.data_handler.calculate_parts_to_sizes(comms=comms)
        query_handler.calculate_parts_to_sizes(comms=comms)

        data_parts_to_ranks, data_nrows = \
            parts_to_ranks(self.client,
                           worker_info,
                           self.data_handler.gpu_futures)

        query_parts_to_ranks, query_nrows = \
            parts_to_ranks(self.client,
                           worker_info,
                           query_handler.gpu_futures)

        """
        Each Dask worker creates a single model
        """
        key = uuid1()
        models = dict([(worker, self.client.submit(
            self._func_create_model,
            comms.sessionId,
            **self.kwargs,
            workers=[worker],
            key="%s-%s" % (key, idx)))
            for idx, worker in enumerate(comms.worker_addresses)])

        """
        Invoke knn_classify on Dask workers to perform distributed query
        """
        key = uuid1()
        knn_prob_res = dict([(worker_info[worker]["rank"], self.client.submit(
                            self._func_predict,
                            models[worker],
                            self.data_handler.worker_to_parts[worker] if
                            worker in self.data_handler.workers else [],
                            data_parts_to_ranks,
                            data_nrows,
                            query_handler.worker_to_parts[worker] if
                            worker in query_handler.workers else [],
                            query_parts_to_ranks,
                            query_nrows,
                            self.uniq_labels,
                            self.n_unique,
                            X.shape[1],
                            worker_info[worker]["rank"],
                            convert_dtype,
                            True,
                            key="%s-%s" % (key, idx),
                            workers=[worker]))
                           for idx, worker in enumerate(comms.worker_addresses)
                            ])

        wait(list(knn_prob_res.values()))
        raise_exception_from_futures(list(knn_prob_res.values()))

        n_outputs = len(self.n_unique)

        """
        Gather resulting partitions and return result
        """
        outputs = []
        for o in range(n_outputs):
            futures = flatten_grouped_results(self.client,
                                              query_parts_to_ranks,
                                              knn_prob_res,
                                              getter_func=_custom_getter(o))
            outputs.append(to_output(futures, self.datatype))

        comms.destroy()

        return tuple(outputs)