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

import pytest

from cuml.datasets import make_classification
from ..thirdparty_adapters import to_output_type
from numpy.testing import assert_allclose as np_assert_allclose

import numpy as np
import cupy as cp
from cupy.sparse import csr_matrix as gpu_csr_matrix
from cupy.sparse import csc_matrix as gpu_csc_matrix
from scipy.sparse import csr_matrix as cpu_csr_matrix
from scipy.sparse import csc_matrix as cpu_csc_matrix


def create_rand_clf():
    clf, _ = make_classification(n_samples=500,
                                 n_features=20,
                                 n_clusters_per_class=1,
                                 n_informative=12,
                                 n_classes=5,
                                 order='F')
    return clf


def create_rand_integers():
    randint = cp.random.randint(30, size=(500, 20)).astype(cp.float64)
    randint = cp.asfortranarray(randint)
    return randint


def convert(dataset, conversion_format):
    converted_dataset = to_output_type(dataset, conversion_format)
    dataset = cp.asnumpy(dataset)
    return dataset, converted_dataset


def sparsify_and_convert(dataset, conversion_format):
    random_loc = cp.random.choice(dataset.size,
                                  int(dataset.size * 0.3),
                                  replace=False)
    dataset.ravel()[random_loc] = 0

    if conversion_format == "scipy-csr":
        dataset = cp.asnumpy(dataset)
        converted_dataset = cpu_csr_matrix(dataset)
    elif conversion_format == "scipy-csc":
        dataset = cp.asnumpy(dataset)
        converted_dataset = cpu_csc_matrix(dataset)
    elif conversion_format == "cupy-csr":
        converted_dataset = gpu_csr_matrix(dataset)
        dataset = cp.asnumpy(dataset)
    elif conversion_format == "cupy-csc":
        converted_dataset = gpu_csc_matrix(dataset)
        dataset = cp.asnumpy(dataset)
    return cpu_csr_matrix(dataset), converted_dataset


@pytest.fixture(scope="session",
                params=["numpy", "dataframe", "cupy", "cudf", "numba"])
def clf_dataset(request):
    clf = create_rand_clf()
    return convert(clf, request.param)


@pytest.fixture(scope="session",
                params=["numpy", "dataframe", "cupy", "cudf", "numba"])
def int_dataset(request):
    randint = create_rand_integers()
    return convert(randint, request.param)


@pytest.fixture(scope="session",
                params=["scipy-csr", "scipy-csc", "cupy-csr", "cupy-csc"])
def sparse_clf_dataset(request):
    clf = create_rand_clf()
    return sparsify_and_convert(clf, request.param)


@pytest.fixture(scope="session",
                params=["scipy-csr", "scipy-csc", "cupy-csr", "cupy-csc"])
def sparse_int_dataset(request):
    randint = create_rand_integers()
    return sparsify_and_convert(randint, request.param)


def assert_allclose(actual, desired, rtol=1e-05, atol=1e-05):
    if not isinstance(actual, np.ndarray):
        actual = to_output_type(actual, 'numpy')
    if not isinstance(desired, np.ndarray):
        desired = to_output_type(desired, 'numpy')
    return np_assert_allclose(actual, desired, rtol=rtol, atol=atol)
