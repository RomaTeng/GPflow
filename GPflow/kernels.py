# Copyright 2016 James Hensman, Valentine Svensson, alexggmatthews
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import print_function, absolute_import
from functools import reduce
import itertools
import warnings

import tensorflow as tf
import numpy as np
from .param import Param, Parameterized, AutoFlow
from . import transforms
from ._settings import settings

float_type = settings.dtypes.float_type
int_type = settings.dtypes.int_type
np_float_type = np.float32 if float_type is tf.float32 else np.float64


def hermgauss(n):
    x, w = np.polynomial.hermite.hermgauss(n)
    x, w = x.astype(np_float_type), w.astype(np_float_type)
    return x, w


def mvhermgauss(means, covs, H, D):
    """
    Return the evaluation locations, and weights for several multivariate Hermite-Gauss quadrature runs.
    :param means: NxD
    :param covs: NxDxD
    :param H: Number of Gauss-Hermite evaluation points.
    :param D: Number of input dimensions. Needs to be known at call-time.
    :return: eval_locations (H**D*NxD), weights (H**D)
    """
    N = tf.shape(means)[0]
    gh_x, gh_w = hermgauss(H)
    xn = np.array(list(itertools.product(*(gh_x,) * D)))  # H**DxD
    wn = np.prod(np.array(list(itertools.product(*(gh_w,) * D))), 1)  # H**D
    cholXcov = tf.cholesky(covs)  # NxDxD
    X = 2.0 ** 0.5 * tf.batch_matmul(cholXcov, tf.tile(xn[None, :, :], (N, 1, 1)),
                                     adj_y=True) + tf.expand_dims(means, 2)  # NxDxH**D
    Xr = tf.reshape(tf.transpose(X, [2, 0, 1]), (-1, D))  # (H**D*N)xD
    return Xr, wn * np.pi ** (-D * 0.5)


class Kern(Parameterized):
    """
    The basic kernel class. Handles input_dim and active dims, and provides a
    generic '_slice' function to implement them.
    """

    def __init__(self, input_dim, active_dims=None):
        """
        input dim is an integer
        active dims is either an iterable of integers or None.

        Input dim is the number of input dimensions to the kernel. If the
        kernel is computed on a matrix X which has more columns than input_dim,
        then by default, only the first input_dim columns are used. If
        different columns are required, then they may be specified by
        active_dims.

        If active dims is None, it effectively defaults to range(input_dim),
        but we store it as a slice for efficiency.
        """
        Parameterized.__init__(self)
        self.scoped_keys.extend(['K', 'Kdiag'])
        self.input_dim = int(input_dim)
        if active_dims is None:
            self.active_dims = slice(input_dim)
        elif type(active_dims) is slice:
            self.active_dims = active_dims
            if active_dims.start is not None and active_dims.stop is not None and active_dims.step is not None:
                assert len(range(*active_dims)) == input_dim  # pragma: no cover
        else:
            self.active_dims = np.array(active_dims, dtype=np.int32)
            assert len(active_dims) == input_dim

        self.num_gauss_hermite_points = 20

    def _slice(self, X, X2):
        """
        Slice the correct dimensions for use in the kernel, as indicated by
        `self.active_dims`.
        :param X: Input 1 (NxD).
        :param X2: Input 2 (MxD), may be None.
        :return: Sliced X, X2, (Nxself.input_dim).
        """
        if isinstance(self.active_dims, slice):
            X = X[:, self.active_dims]
            if X2 is not None:
                X2 = X2[:, self.active_dims]
        else:
            X = tf.transpose(tf.gather(tf.transpose(X), self.active_dims))
            if X2 is not None:
                X2 = tf.transpose(tf.gather(tf.transpose(X2), self.active_dims))
        with tf.control_dependencies([
            tf.assert_equal(tf.shape(X)[1], tf.constant(self.input_dim, dtype=settings.dtypes.int_type))
        ]):
            X = tf.identity(X)

        return X, X2

    def _slice_cov(self, cov):
        """
        Slice the correct dimensions for use in the kernel, as indicated by
        `self.active_dims` for covariance matrices. This requires slicing the
        rows *and* columns. This will also turn flattened diagonal
        matrices into a tensor of full diagonal matrices.
        :param cov: Tensor of covariance matrices (NxDxD or NxD).
        :return: N x self.input_dim x self.input_dim.
        """
        cov = tf.cond(tf.equal(tf.rank(cov), 2), lambda: tf.matrix_diag(cov), lambda: cov)

        if isinstance(self.active_dims, slice):
            cov = cov[..., self.active_dims, self.active_dims]
        else:
            cov_shape = tf.shape(cov)
            covr = tf.reshape(cov, [-1, cov_shape[-1], cov_shape[-1]])
            gather1 = tf.gather(tf.transpose(covr, [2, 1, 0]), self.active_dims)
            gather2 = tf.gather(tf.transpose(gather1, [1, 0, 2]), self.active_dims)
            cov = tf.reshape(tf.transpose(gather2, [2, 0, 1]),
                             tf.concat(0, [cov_shape[:-2], [len(self.active_dims), len(self.active_dims)]]))
        return cov

    def __add__(self, other):
        return Add([self, other])

    def __mul__(self, other):
        return Prod([self, other])

    @AutoFlow((float_type, [None, None]), (float_type, [None, None]))
    def compute_K(self, X, Z):
        return self.K(X, Z)

    @AutoFlow((float_type, [None, None]))
    def compute_K_symm(self, X):
        return self.K(X)

    @AutoFlow((float_type, [None, None]))
    def compute_Kdiag(self, X):
        return self.Kdiag(X)

    @AutoFlow((float_type, [None, None]), (float_type,))
    def compute_eKdiag(self, X, Xcov=None):
        return self.eKdiag(X, Xcov)

    @AutoFlow((float_type, [None, None]), (float_type, [None, None]), (float_type,))
    def compute_eKxz(self, Z, Xmu, Xcov):
        return self.eKxz(Z, Xmu, Xcov)

    @AutoFlow((float_type, [None, None]), (float_type, [None, None]), (float_type, [None, None, None, None]))
    def compute_exKxz(self, Z, Xmu, Xcov):
        return self.exKxz(Z, Xmu, Xcov)

    @AutoFlow((float_type, [None, None]), (float_type, [None, None]), (float_type,))
    def compute_eKzxKxz(self, Z, Xmu, Xcov):
        return self.eKzxKxz(Z, Xmu, Xcov)

    def _check_quadrature(self):
        if settings.numerics.ekern_quadrature == "warn":
            warnings.warn("Using numerical quadrature for kernel expectation of %s. Use GPflow.ekernels instead." %
                          str(type(self)))
        if settings.numerics.ekern_quadrature == "error" or self.num_gauss_hermite_points == 0:
            raise RuntimeError("Settings indicate that quadrature may not be used.")

    def eKdiag(self, Xmu, Xcov):
        """
        Computes <K_xx>_q(x).
        :param Xmu: Mean (NxD)
        :param Xcov: Covariance (NxDxD or NxD)
        :return: (N)
        """
        self._check_quadrature()
        Xmu, _ = self._slice(Xmu, None)
        Xcov = self._slice_cov(Xcov)
        X, wn = mvhermgauss(Xmu, Xcov, self.num_gauss_hermite_points, self.input_dim)  # (H**DxNxD, H**D)
        Kdiag = tf.reshape(self.Kdiag(X, presliced=True),
                           (self.num_gauss_hermite_points ** self.input_dim, tf.shape(Xmu)[0]))
        eKdiag = tf.reduce_sum(Kdiag * wn[:, None], 0)
        return eKdiag  # N

    def eKxz(self, Z, Xmu, Xcov):
        """
        Computes <K_xz>_q(x) using quadrature.
        :param Z: Fixed inputs (MxD).
        :param Xmu: X means (NxD).
        :param Xcov: X covariances (NxDxD or NxD).
        :return: (NxM)
        """
        self._check_quadrature()
        Xmu, Z = self._slice(Xmu, Z)
        Xcov = self._slice_cov(Xcov)
        N = tf.shape(Xmu)[0]
        M = tf.shape(Z)[0]
        HpowD = self.num_gauss_hermite_points ** self.input_dim
        X, wn = mvhermgauss(Xmu, Xcov, self.num_gauss_hermite_points, self.input_dim)  # (H**DxNxD, H**D)
        Kxz = tf.reshape(self.K(tf.reshape(X, (-1, self.input_dim)), Z, presliced=True), (HpowD, N, M))
        eKxz = tf.reduce_sum(Kxz * wn[:, None, None], 0)
        return eKxz

    def exKxz(self, Z, Xmu, Xcov):
        """
        Computes <x_{t-1} K_{x_t z}>_q(x) for each pair of consecutive X's in
        Xmu & Xcov.
        :param Z: Fixed inputs (MxD).
        :param Xmu: X means (T+1xD).
        :param Xcov: 2xT+1xDxD. [0, t, :, :] contains covariances for x_t. [1, t, :, :] contains the cross covariances
        for t and t+1.
        :return: (TxMxD).
        """
        self._check_quadrature()
        # Slicing is NOT needed here. The desired behaviour is to *still* return an NxMxD matrix. As even when the
        # kernel does not depend on certain inputs, the output matrix will still contain the outer product between the
        # mean of x_{t-1} and K_{x_t Z}. The code here will do this correctly automatically, since the quadrature will
        # still be done over the distribution x_{t-1, t}, only now the kernel will not depend on certain inputs.
        # However, this does mean that at the time of running this function we need to know the input *size* of Xmu, not
        # just `input_dim`.
        N = tf.shape(Xmu)[0] - 1
        M = tf.shape(Z)[0]
        D = self.input_size if hasattr(self, 'input_size') else self.input_dim  # Number of actual input dimensions
        Hpow2D = self.num_gauss_hermite_points ** (2 * D)

        with tf.control_dependencies([
            tf.assert_equal(tf.shape(Xmu)[1], tf.constant(D, dtype=int_type),
                            message="Numerical quadrature needs to know correct shape of Xmu.")
        ]):
            Xmu = tf.identity(Xmu)

        # First, transform the compact representation of Xmu and Xcov into a list of full distributions.
        fXmu = tf.concat(1, (Xmu[:-1, :], Xmu[1:, :]))  # Nx2D
        fXcovt = tf.concat(2, (Xcov[0, :-1, :, :], Xcov[1, :-1, :, :]))  # NxDx2D
        fXcovb = tf.concat(2, (tf.transpose(Xcov[1, :-1, :, :], (0, 2, 1)), Xcov[0, 1:, :, :]))
        fXcov = tf.concat(1, (fXcovt, fXcovb))  # Confirmed correct
        X, wn = mvhermgauss(fXmu, fXcov, self.num_gauss_hermite_points, D * 2)  # (H**2DxNx2D, H**2D)
        Kxz = tf.reshape(self.K(X[:, :D], Z), (Hpow2D, N, M))
        exKxz = tf.reduce_sum(
            tf.expand_dims(tf.reshape(X[:, D:], (Hpow2D, N, D)), 2) * tf.expand_dims(Kxz, 3) * wn[:, None, None, None],
            0)
        return exKxz

    def eKzxKxz(self, Z, Xmu, Xcov):
        """
        Computes <K_zx Kxz>_q(x).
        :param Z: Fixed inputs.
        :param Xmu: X means (NxD).
        :param Xcov: X covariances (NxDxD or NxD).
        :return: NxDxD
        """
        self._check_quadrature()
        Xmu, Z = self._slice(Xmu, Z)
        Xcov = self._slice_cov(Xcov)
        N = tf.shape(Xmu)[0]
        M = tf.shape(Z)[0]
        HpowD = self.num_gauss_hermite_points ** self.input_dim
        X, wn = mvhermgauss(Xmu, Xcov, self.num_gauss_hermite_points, self.input_dim)  # (H**DxNxD, H**D)
        Kxz = tf.reshape(self.K(tf.reshape(X, (-1, self.input_dim)), Z, presliced=True), (HpowD, N, M))
        KzxKxz = tf.expand_dims(Kxz, 3) * tf.expand_dims(Kxz, 2)
        eKzxKxz = tf.reduce_sum(KzxKxz * wn[:, None, None, None], 0)
        return eKzxKxz


class Static(Kern):
    """
    Kernels who don't depend on the value of the inputs are 'Static'.  The only
    parameter is a variance.
    """

    def __init__(self, input_dim, variance=1.0, active_dims=None):
        Kern.__init__(self, input_dim, active_dims)
        self.variance = Param(variance, transforms.positive)

    def Kdiag(self, X):
        return tf.fill(tf.pack([tf.shape(X)[0]]), tf.squeeze(self.variance))


class White(Static):
    """
    The White kernel
    """

    def K(self, X, X2=None, presliced=False):
        if X2 is None:
            d = tf.fill(tf.pack([tf.shape(X)[0]]), tf.squeeze(self.variance))
            return tf.diag(d)
        else:
            shape = tf.pack([tf.shape(X)[0], tf.shape(X2)[0]])
            return tf.zeros(shape, float_type)


class Constant(Static):
    """
    The Constant (aka Bias) kernel
    """

    def K(self, X, X2=None, presliced=False):
        if X2 is None:
            shape = tf.pack([tf.shape(X)[0], tf.shape(X)[0]])
        else:
            shape = tf.pack([tf.shape(X)[0], tf.shape(X2)[0]])
        return tf.fill(shape, tf.squeeze(self.variance))


class Bias(Constant):
    """
    Another name for the Constant kernel, included for convenience.
    """
    pass


class Stationary(Kern):
    """
    Base class for kernels that are stationary, that is, they only depend on

        r = || x - x' ||

    This class handles 'ARD' behaviour, which stands for 'Automatic Relevance
    Determination'. This means that the kernel has one lengthscale per
    dimension, otherwise the kernel is isotropic (has a single lengthscale).
    """

    def __init__(self, input_dim, variance=1.0, lengthscales=None,
                 active_dims=None, ARD=False):
        """
        - input_dim is the dimension of the input to the kernel
        - variance is the (initial) value for the variance parameter
        - lengthscales is the initial value for the lengthscales parameter
          defaults to 1.0 (ARD=False) or np.ones(input_dim) (ARD=True).
        - active_dims is a list of length input_dim which controls which
          columns of X are used.
        - ARD specifies whether the kernel has one lengthscale per dimension
          (ARD=True) or a single lengthscale (ARD=False).
        """
        Kern.__init__(self, input_dim, active_dims)
        self.scoped_keys.extend(['square_dist', 'euclid_dist'])
        self.variance = Param(variance, transforms.positive)
        if ARD:
            if lengthscales is None:
                lengthscales = np.ones(input_dim, np_float_type)
            else:
                # accepts float or array:
                lengthscales = lengthscales * np.ones(input_dim, np_float_type)
            self.lengthscales = Param(lengthscales, transforms.positive)
            self.ARD = True
        else:
            if lengthscales is None:
                lengthscales = 1.0
            self.lengthscales = Param(lengthscales, transforms.positive)
            self.ARD = False

    def square_dist(self, X, X2):
        X = X / self.lengthscales
        Xs = tf.reduce_sum(tf.square(X), 1)
        if X2 is None:
            return -2 * tf.matmul(X, tf.transpose(X)) + \
                   tf.reshape(Xs, (-1, 1)) + tf.reshape(Xs, (1, -1))
        else:
            X2 = X2 / self.lengthscales
            X2s = tf.reduce_sum(tf.square(X2), 1)
            return -2 * tf.matmul(X, tf.transpose(X2)) + \
                   tf.reshape(Xs, (-1, 1)) + tf.reshape(X2s, (1, -1))

    def euclid_dist(self, X, X2):
        r2 = self.square_dist(X, X2)
        return tf.sqrt(r2 + 1e-12)

    def Kdiag(self, X, presliced=False):
        return tf.fill(tf.pack([tf.shape(X)[0]]), tf.squeeze(self.variance))


class RBF(Stationary):
    """
    The radial basis function (RBF) or squared exponential kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        return self.variance * tf.exp(-self.square_dist(X, X2) / 2)


class Linear(Kern):
    """
    The linear kernel
    """

    def __init__(self, input_dim, variance=1.0, active_dims=None, ARD=False):
        """
        - input_dim is the dimension of the input to the kernel
        - variance is the (initial) value for the variance parameter(s)
          if ARD=True, there is one variance per input
        - active_dims is a list of length input_dim which controls
          which columns of X are used.
        """
        Kern.__init__(self, input_dim, active_dims)
        self.ARD = ARD
        if ARD:
            # accept float or array:
            variance = np.ones(self.input_dim) * variance
            self.variance = Param(variance, transforms.positive)
        else:
            self.variance = Param(variance, transforms.positive)
        self.parameters = [self.variance]

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        if X2 is None:
            return tf.matmul(X * self.variance, tf.transpose(X))
        else:
            return tf.matmul(X * self.variance, tf.transpose(X2))

    def Kdiag(self, X, presliced=False):
        if not presliced:
            X, _ = self._slice(X, None)
        return tf.reduce_sum(tf.square(X) * self.variance, 1)


class Polynomial(Linear):
    """
    The Polynomial kernel. Samples are polynomials of degree `d`.
    """

    def __init__(self, input_dim, degree=3.0, variance=1.0, offset=1.0, active_dims=None, ARD=False):
        """
        :param input_dim: the dimension of the input to the kernel
        :param variance: the (initial) value for the variance parameter(s)
                         if ARD=True, there is one variance per input
        :param degree: the degree of the polynomial
        :param active_dims: a list of length input_dim which controls
          which columns of X are used.
        :param ARD: use variance as described
        """
        Linear.__init__(self, input_dim, variance, active_dims, ARD)
        self.degree = degree
        self.offset = Param(offset, transform=transforms.positive)

    def K(self, X, X2=None, presliced=False):
        return (Linear.K(self, X, X2, presliced=presliced) + self.offset) ** self.degree

    def Kdiag(self, X, presliced=False):
        return (Linear.Kdiag(self, X, presliced=presliced) + self.offset) ** self.degree


class Exponential(Stationary):
    """
    The Exponential kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        r = self.euclid_dist(X, X2)
        return self.variance * tf.exp(-0.5 * r)


class Matern12(Stationary):
    """
    The Matern 1/2 kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        r = self.euclid_dist(X, X2)
        return self.variance * tf.exp(-r)


class Matern32(Stationary):
    """
    The Matern 3/2 kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        r = self.euclid_dist(X, X2)
        return self.variance * (1. + np.sqrt(3.) * r) * \
               tf.exp(-np.sqrt(3.) * r)


class Matern52(Stationary):
    """
    The Matern 5/2 kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        r = self.euclid_dist(X, X2)
        return self.variance * (1.0 + np.sqrt(5.) * r + 5. / 3. * tf.square(r)) \
               * tf.exp(-np.sqrt(5.) * r)


class Cosine(Stationary):
    """
    The Cosine kernel
    """

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        r = self.euclid_dist(X, X2)
        return self.variance * tf.cos(r)


class PeriodicKernel(Kern):
    """
    The periodic kernel. Defined in  Equation (47) of

    D.J.C.MacKay. Introduction to Gaussian processes. In C.M.Bishop, editor,
    Neural Networks and Machine Learning, pages 133--165. Springer, 1998.

    Derived using the mapping u=(cos(x), sin(x)) on the inputs.
    """

    def __init__(self, input_dim, period=1.0, variance=1.0,
                 lengthscales=1.0, active_dims=None):
        # No ARD support for lengthscale or period yet
        Kern.__init__(self, input_dim, active_dims)
        self.variance = Param(variance, transforms.positive)
        self.lengthscales = Param(lengthscales, transforms.positive)
        self.ARD = False
        self.period = Param(period, transforms.positive)

    def Kdiag(self, X, presliced=False):
        return tf.fill(tf.pack([tf.shape(X)[0]]), tf.squeeze(self.variance))

    def K(self, X, X2=None, presliced=False):
        if not presliced:
            X, X2 = self._slice(X, X2)
        if X2 is None:
            X2 = X

        # Introduce dummy dimension so we can use broadcasting
        f = tf.expand_dims(X, 1)  # now N x 1 x D
        f2 = tf.expand_dims(X2, 0)  # now 1 x M x D

        r = np.pi * (f - f2) / self.period
        r = tf.reduce_sum(tf.square(tf.sin(r) / self.lengthscales), 2)

        return self.variance * tf.exp(-0.5 * r)


class Coregion(Kern):
    def __init__(self, input_dim, output_dim, rank, active_dims=None):
        """
        A Coregionalization kernel. The inputs to this kernel are _integers_
        (we cast them from floats as needed) which usually specify the
        *outputs* of a Coregionalization model.

        The parameters of this kernel, W, kappa, specify a positive-definite
        matrix B.

          B = W W^T + diag(kappa) .

        The kernel function is then an indexing of this matrix, so

          K(x, y) = B[x, y] .

        We refer to the size of B as "num_outputs x num_outputs", since this is
        the number of outputs in a coregionalization model. We refer to the
        number of columns on W as 'rank': it is the number of degrees of
        correlation between the outputs.

        NB. There is a symmetry between the elements of W, which creates a
        local minimum at W=0. To avoid this, it's recommended to initialize the
        optimization (or MCMC chain) using a random W.
        """
        assert input_dim == 1, "Coregion kernel in 1D only"
        Kern.__init__(self, input_dim, active_dims)

        self.output_dim = output_dim
        self.rank = rank
        self.W = Param(np.zeros((self.output_dim, self.rank)))
        self.kappa = Param(np.ones(self.output_dim), transforms.positive)

    def K(self, X, X2=None):
        X, X2 = self._slice(X, X2)
        X = tf.cast(X[:, 0], tf.int32)
        if X2 is None:
            X2 = X
        else:
            X2 = tf.cast(X2[:, 0], tf.int32)
        B = tf.matmul(self.W, tf.transpose(self.W)) + tf.diag(self.kappa)
        return tf.gather(tf.transpose(tf.gather(B, X2)), X)

    def Kdiag(self, X):
        X, _ = self._slice(X, None)
        X = tf.cast(X[:, 0], tf.int32)
        Bdiag = tf.reduce_sum(tf.square(self.W), 1) + self.kappa
        return tf.gather(Bdiag, X)


def make_kernel_names(kern_list):
    """
    Take a list of kernels and return a list of strings, giving each kernel a
    unique name.

    Each name is made from the lower-case version of the kernel's class name.

    Duplicate kernels are given trailing numbers.
    """
    names = []
    counting_dict = {}
    for k in kern_list:
        raw_name = k.__class__.__name__.lower()

        # check for duplicates: start numbering if needed
        if raw_name in counting_dict:
            if counting_dict[raw_name] == 1:
                names[names.index(raw_name)] = raw_name + '_1'
            counting_dict[raw_name] += 1
            name = raw_name + '_' + str(counting_dict[raw_name])
        else:
            counting_dict[raw_name] = 1
            name = raw_name
        names.append(name)
    return names


class Combination(Kern):
    """
    Combine  a list of kernels, e.g. by adding or multiplying (see inheriting
    classes).

    The names of the kernels to be combined are generated from their class
    names.
    """

    def __init__(self, kern_list):
        for k in kern_list:
            assert isinstance(k, Kern), "can only add Kern instances"

        input_dim = np.max([k.input_dim
                            if type(k.active_dims) is slice else
                            np.max(k.active_dims) + 1
                            for k in kern_list])
        Kern.__init__(self, input_dim=input_dim)

        # add kernels to a list, flattening out instances of this class therein
        self.kern_list = []
        for k in kern_list:
            if isinstance(k, self.__class__):
                self.kern_list.extend(k.kern_list)
            else:
                self.kern_list.append(k)

        # generate a set of suitable names and add the kernels as attributes
        names = make_kernel_names(self.kern_list)
        [setattr(self, name, k) for name, k in zip(names, self.kern_list)]

    @property
    def on_separate_dimensions(self):
        """
        Checks whether the kernels in the combination act on disjoint subsets
        of dimensions. Currently, it is hard to asses whether two slice objects
        will overlap, so this will always return False.
        :return: Boolean indicator.
        """
        if np.any([isinstance(k.active_dims, slice) for k in self.kern_list]):
            # Be conservative in the case of a slice object
            return False
        else:
            dimlist = [k.active_dims for k in self.kern_list]
            overlapping = False
            for i, dims_i in enumerate(dimlist):
                for dims_j in dimlist[i + 1:]:
                    if np.any(dims_i.reshape(-1, 1) == dims_j.reshape(1, -1)):
                        overlapping = True
            return not overlapping


class Add(Combination):
    def K(self, X, X2=None, presliced=False):
        return reduce(tf.add, [k.K(X, X2) for k in self.kern_list])

    def Kdiag(self, X, presliced=False):
        return reduce(tf.add, [k.Kdiag(X) for k in self.kern_list])


class Prod(Combination):
    def K(self, X, X2=None, presliced=False):
        return reduce(tf.mul, [k.K(X, X2) for k in self.kern_list])

    def Kdiag(self, X, presliced=False):
        return reduce(tf.mul, [k.Kdiag(X) for k in self.kern_list])
