from __future__ import print_function, division, absolute_import

from .base import NNOps, nnops_initscope

from odin import backend as K
from odin.basic import (BatchNormPopulationMean, BatchNormScaleParameter,
                        BatchNormPopulationInvStd, BatchNormShiftParameter,
                        add_updates)


class BatchNorm(NNOps):
    """ This class is adpated from Lasagne:
    Original work Copyright (c) 2014-2015 lasagne contributors
    All rights reserved.
    LICENSE: https://github.com/Lasagne/Lasagne/blob/master/LICENSE

    Batch Normalization

    This layer implements batch normalization of its inputs, following [1]_:

    .. math::
        y = \\frac{x - \\mu}{\\sqrt{\\sigma^2 + \\epsilon}} \\gamma + \\beta

    That is, the input is normalized to zero mean and unit variance, and then
    linearly transformed. The crucial part is that the mean and variance are
    computed across the batch dimension, i.e., over examples, not per example.

    During training, :math:`\\mu` and :math:`\\sigma^2` are defined to be the
    mean and variance of the current input mini-batch :math:`x`, and during
    testing, they are replaced with average statistics over the training
    data. Consequently, this layer has four stored parameters: :math:`\\beta`,
    :math:`\\gamma`, and the averages :math:`\\mu` and :math:`\\sigma^2`
    (nota bene: instead of :math:`\\sigma^2`, the layer actually stores
    :math:`1 / \\sqrt{\\sigma^2 + \\epsilon}`, for compatibility to cuDNN).
    By default, this layer learns the average statistics as exponential moving
    averages computed during training, so it can be plugged into an existing
    network without any changes of the training procedure (see Notes).

    Parameters
    ----------
    axes : 'auto', int or tuple of int
        The axis or axes to normalize over. If ``'auto'`` (the default),
        normalize over all axes except for the final (often used to represent
        channel or feature map).
    epsilon : scalar
        Small constant :math:`\\epsilon` added to the variance before taking
        the square root and dividing by it, to avoid numerical problems
    alpha : scalar
        Coefficient for the exponential moving average of batch-wise means and
        standard deviations computed during training; the closer to one, the
        more it will depend on the last batches seen
    beta : trainable variable, expression, numpy array, callable or None
        Initial value, expression or initializer for :math:`\\beta`. Must match
        the incoming shape, skipping all axes in `axes`. Set to ``None`` to fix
        it to 0.0 instead of learning it.
        See :func:`lasagne.utils.create_param` for more information.
    gamma : trainable variable, expression, numpy array, callable or None
        Initial value, expression or initializer for :math:`\\gamma`. Must
        match the incoming shape, skipping all axes in `axes`. Set to ``None``
        to fix it to 1.0 instead of learning it.
        See :func:`lasagne.utils.create_param` for more information.
    mean : Theano shared variable, expression, numpy array, or callable
        Initial value, expression or initializer for :math:`\\mu`. Must match
        the incoming shape, skipping all axes in `axes`.
        See :func:`lasagne.utils.create_param` for more information.
    inv_std : Theano shared variable, expression, numpy array, or callable
        Initial value, expression or initializer for :math:`1 / \\sqrt{
        \\sigma^2 + \\epsilon}`. Must match the incoming shape, skipping all
        axes in `axes`.
        See :func:`lasagne.utils.create_param` for more information.
    noise_level : None, float or tensor scalar
        if `noise_level` is not None, it specify standard deviation of
        added Gaussian noise. The noise will be applied before adding `beta`.
    noise_dims: int, list(int), or 'auto'
        these dimensions will be setted to 1 in noise_shape, and
        used to broadcast the dropout mask.
        If `noise_dims` is "auto", it will be the same as `axes`
    **kwargs
        Any additional keyword arguments are passed to the :class:`Layer`
        superclass.

    Notes
    -----
    This layer should be inserted between a linear transformation (such as a
    :class:`DenseLayer`, or :class:`Conv2DLayer`) and its activation. The
    convenience function :func:`batch_norm` modifies an existing layer to
    insert batch normalization in front of its activation.

    The behavior can be controlled by passing keyword arguments to
    :func:`lasagne.layers.get_output()` when building the output expression
    of any network containing this layer.

    During training, [1]_ normalize each input mini-batch by its statistics
    and update an exponential moving average of the statistics to be used for
    validation. This can be achieved by passing ``deterministic=False``.
    For validation, [1]_ normalize each input mini-batch by the stored
    statistics. This can be achieved by passing ``deterministic=True``.

    For more fine-grained control, ``batch_norm_update_averages`` can be passed
    to update the exponential moving averages (``True``) or not (``False``),
    and ``batch_norm_use_averages`` can be passed to use the exponential moving
    averages for normalization (``True``) or normalize each mini-batch by its
    own statistics (``False``). These settings override ``deterministic``.

    Note that for testing a model after training, [1]_ replace the stored
    exponential moving average statistics by fixing all network weights and
    re-computing average statistics over the training data in a layerwise
    fashion. This is not part of the layer implementation.

    In case you set `axes` to not include the batch dimension (the first axis,
    usually), normalization is done per example, not across examples. This does
    not require any averages, so you can pass ``batch_norm_update_averages``
    and ``batch_norm_use_averages`` as ``False`` in this case.

    Example
    --------
    For convolution output with the shape
    (nb_samples, nb_channel, width, height) you can normalize axes=(0, 1, 2, 3)
    or any subset of above axes, but you must include the first dimension (0,)
    because we don't know the batch size in advance.


    References
    ----------
    .. [1] Ioffe, Sergey and Szegedy, Christian (2015):
           Batch Normalization: Accelerating Deep Network Training by Reducing
           Internal Covariate Shift. http://arxiv.org/abs/1502.03167.
    """

    @nnops_initscope
    def __init__(self, axes='auto', epsilon=1e-4, alpha=0.1,
                 beta_init=K.init.constant(0), gamma_init=K.init.constant(1),
                 mean_init=K.init.constant(0), inv_std_init=K.init.constant(1),
                 noise_level=None, noise_dims='auto', activation=K.linear, **kwargs):
        super(BatchNorm, self).__init__(**kwargs)
        self.axes = axes
        self.epsilon = epsilon
        self.alpha = alpha
        self.beta_init = beta_init
        self.gamma_init = gamma_init
        self.mean_init = mean_init
        self.inv_std_init = inv_std_init
        # ====== noise ====== #
        self.noise_level = noise_level
        self.noise_dims = noise_dims
        self.activation = K.linear if activation is None else activation

    # ==================== abstract method ==================== #
    def _initialize(self):
        if self.axes == 'auto':
            # default: normalize over all but the second axis
            # for so-called "global normalization", used with convolutional
            # filters with shape [batch, height, width, depth],
            # pass axes=[0, 1, 2]
            self.axes = tuple(range(0, len(self.input_shape) - 1))
        elif isinstance(self.axes, int):
            self.axes = (self.axes,)
        # check noise_dims
        if self.noise_dims == 'auto':
            self.noise_dims = self.axes
        # create parameters, ignoring all dimensions in axes
        shape = [size for axis, size in enumerate(self.input_shape)
                 if axis not in self.axes]
        if any(size is None for size in shape):
            raise ValueError("BatchNorm needs specified input sizes for "
                             "all axes not normalized over.")
        # init learnable parameters
        if self.beta_init is not None:
            self.config.create_params(
                self.beta_init, shape=shape, name='beta',
                roles=BatchNormShiftParameter)
        if self.gamma_init is not None:
            self.config.create_params(
                self.gamma_init, shape=shape, name='gamma',
                roles=BatchNormScaleParameter)
        # running mean and invert std
        if self.mean_init is not None:
            self.config.create_params(
                self.mean_init, shape=shape, name='mean',
                roles=BatchNormPopulationMean)
        if self.inv_std_init is not None:
            self.config.create_params(
                self.inv_std_init, shape=shape, name='inv_std',
                roles=BatchNormPopulationInvStd)

    def _apply(self, X, noise=0):
        input_shape = K.get_shape(X)
        ndim = K.ndim(X)
        is_training = K.is_training()
        # if is training, normalize input by its own mean and std
        mean = (K.mean(X, self.axes)
            if is_training or not hasattr(self, 'mean') else self.mean)
        inv_std = (K.inv(K.sqrt(K.var(X, self.axes) + self.epsilon))
            if is_training or not hasattr(self, 'inv_std') else self.inv_std)
        # set a default update for them:
        if is_training:
            if hasattr(self, 'mean'):
                running_mean = ((1 - self.alpha) * self.mean +
                                self.alpha * mean)
            if hasattr(self, 'inv_std'):
                running_inv_std = ((1 - self.alpha) * self.inv_std +
                                   self.alpha * inv_std)
        # prepare dimshuffle pattern inserting broadcastable axes as needed
        param_axes = iter(range(ndim - len(self.axes)))
        pattern = ['x' if input_axis in self.axes
                   else next(param_axes)
                   for input_axis in range(ndim)]
        # apply dimshuffle pattern to all parameters
        beta = 0 if not hasattr(self, 'beta') else K.dimshuffle(self.beta, pattern)
        gamma = 1 if not hasattr(self, 'gamma') else K.dimshuffle(self.gamma, pattern)
        # ====== normalizing the input ====== #
        normalized = (X - K.dimshuffle(mean, pattern)) * \
            (gamma * K.dimshuffle(inv_std, pattern))
        # applying noise if required
        if self.noise_level is not None:
            if noise >= 0:
                training = K.is_training()
                if noise > 0: K.set_training(True)
                normalized = K.apply_noise(normalized, level=self.noise_level,
                             noise_dims=self.noise_dims, noise_type='gaussian')
                K.set_training(training)
        normalized = normalized + beta
        # set shape for output
        K.add_shape(normalized, input_shape)
        # activated output
        output = self.activation(normalized)
        # add updates for final output
        if is_training:
            if hasattr(self, 'mean'):
                add_updates(output, self.mean, running_mean)
            if hasattr(self, 'inv_std'):
                add_updates(output, self.inv_std, running_inv_std)
        return output
