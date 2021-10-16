# Copyright 2021 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================
"""Base class of optimizer.

This is under development, and subject to interface/implementation changes.
"""

import abc
import functools

from keras import backend
from keras import initializers
from keras.optimizer_v2 import learning_rate_schedule
from keras.optimizer_v2 import utils as optimizer_utils
import tensorflow.compat.v2 as tf


class _BaseOptimizer(tf.__internal__.tracking.AutoTrackable):
  """Optimizer base class, which only supports non-distribute use case."""

  def __init__(self, name, clipnorm=None, clipvalue=None, global_clipnorm=None):
    """Create a new Optimizer.

    Args:
      name: String. The name to use for momentum accumulator weights created by
        the optimizer.
      clipnorm: float. If set, the gradient of each weight is individually
        clipped so that its norm is no higher than this value.
      clipvalue: float. If set, the gradient of each weight is clipped to be
        no higher than this value.
      global_clipnorm: float. If set, the gradient of all weights is clipped
        so that their global norm is no higher than this value.
    """
    self._name = name
    self._clipnorm = clipnorm
    self._global_clipnorm = global_clipnorm
    if self._clipnorm is not None and self._global_clipnorm is not None:
      raise ValueError(f"At most one of `clipnorm` and `global_clipnorm` can "
                       f"be set. Received: clipnorm={self.clipnorm}, "
                       f"global_clipnorm={self.global_clipnorm}.")
    self._clipvalue = clipvalue
    with tf.init_scope():
      # Lift the variable creation to init scope to avoid environment issue.
      self._iterations = tf.Variable(0, name="iteration", dtype=tf.int64)

  def _var_key(self, variable):
    """Get a unique identifier of the given variable."""
    # Get the distributed variable if it exists.
    # TODO(b/199214315): replace _unique_id with ref() after fixing ref() issues
    # on AggregatingVariable.
    return variable._unique_id  # pylint: disable=protected-access

  @abc.abstractmethod
  def update_step(self, gradient, variable):
    """Function to update variable value based on given gradients.

    This method must be implemented in customized optimizers.

    Args:
      gradient: backpropagated gradient of the given variable.
      variable: variable whose value needs to be updated.

    Returns:
      An `Operation` that applies the specified gradients.

    """
    raise NotImplementedError

  def _compute_gradients(self, loss, var_list, tape=None):
    if tape is None:
      tape = tf.GradientTape()
    if callable(loss):
      with tape:
        tape.watch(var_list)
        loss = loss()
    grads = tape.gradient(loss, var_list)
    return grads, var_list

  def _clip_gradients(self, grads):
    clipped_grads = []
    if self._clipnorm is not None and self._clipnorm > 0:
      for g in grads:
        if g is None:
          clipped_grads.append(g)
        else:
          clipped_grads.append(tf.clip_by_norm(g, self._clipnorm))
      return clipped_grads

    if self._global_clipnorm is not None and self._global_clipnorm > 0:
      return tf.clip_by_global_norm(grads, self._global_clipnorm)

    if self._clipvalue is not None and self._clipvalue > 0:
      for g in grads:
        if g is None:
          clipped_grads.append(g)
        else:
          clipped_grads.append(tf.clip_by_value(g, self._clipvalue))
      return clipped_grads

    return grads

  @property
  def iterations(self):
    """The number of training steps this `optimizer` has run.

    By default, iterations would be incremented by one every time
    `apply_gradients()` is called.
    """
    return self._iterations

  @property
  def learning_rate(self):
    if not hasattr(self, "_learning_rate") or self._learning_rate is None:
      raise ValueError("Missing learning rate, please set self.learning_rate at"
                       " optimizer creation time.")
    lr = self._learning_rate
    if isinstance(lr, learning_rate_schedule.LearningRateSchedule):
      return lr(self.iterations)  # pylint: disable=not-callable
    return lr

  @learning_rate.setter
  def learning_rate(self, learning_rate):
    if isinstance(self._learning_rate,
                  learning_rate_schedule.LearningRateSchedule):
      raise TypeError("This optimizer was created with a `LearningRateSchedule`"
                      " object as its `learning_rate` constructor argument, "
                      "hence its learning rate is not settable. If you need the"
                      " learning rate to be settable, you should instantiate "
                      "the optimizer with a float `learning_rate` argument.")
    self._learning_rate.assign(learning_rate)

  def _build_learning_rate(self, learning_rate):
    if isinstance(learning_rate, learning_rate_schedule.LearningRateSchedule):
      return learning_rate
    return tf.Variable(learning_rate, dtype=backend.floatx())

  @abc.abstractmethod
  def build(self, var_list):
    """Initialize the optimizer's variables, such as momemtum variables.

    This function has to be implemented by subclass optimizers, and subclass
    optimizers need to call `super().build(var_list)`.

    Args:
      var_list: List of model variables to build optimizers on. For example,
        SGD optimizer with momentum will store one momentum variable
        corresponding to each model variable.
    """
    if not hasattr(self, "_index_dict"):
      self._build_index_dict(var_list)

  def _build_index_dict(self, var_list):
    """Build variable to index dictionary.

    Build a dictionary that maps variable to the index of it in the given
    var_list.

    Args:
      var_list: List of variables to build index dict on.

    Returns:
      None
    """
    self._index_dict = {}
    for i, var in enumerate(var_list):
      var_key = self._var_key(var)
      self._index_dict[var_key] = i

  def add_variable(self,
                   shape,
                   dtype=None,
                   initializer="zeros",
                   name=None):
    """Create an optimizer variable.

    Args:
      shape: A list of integers, a tuple of integers, or a 1-D Tensor of type
        int32. Defaults to scalar if unspecified.
      dtype: The DType of the optimizer variable to be created. Defaults to
        `tf.keras.backend.floatx` if unspecified.
      initializer: string or callable. Initializer instance.
      name: The name of the optimizer variable to be created.

    Returns:
      An optimizer variable, in the format of tf.Variable.

    """
    if isinstance(initializer, str):
      initializer = initializers.get(initializer)
    if dtype is None:
      dtype = backend.floatx()
    if shape is None:
      shape = []
    return tf.Variable(
        initial_value=initializer(shape, dtype), name=name, trainable=False)

  def add_variable_from_reference(self,
                                  model_variable,
                                  variable_name,
                                  initial_value=None):
    """Create an optimizer variable from model variable.

    Create an optimizer variable based on the information of model variable.
    For example, in SGD optimizer momemtum, for each model variable, a
    corresponding momemtum variable is created of the same shape and dtype.

    Args:
      model_variable: The corresponding model variable to the optimizer variable
        to be created.
      variable_name: The name prefix of the optimizer variable to be created.
        The create variables name will follow the pattern
        `{variable_name}/{model_variable.name}`, e.g., `momemtum/dense_1`.
      initial_value: The initial value of the optimizer variable, if None, the
        value will be default to 0.

    Returns:
      An optimizer variable.
    """
    if initial_value is None:
      initial_value = tf.zeros(
          shape=model_variable.shape, dtype=model_variable.dtype)
    return tf.Variable(
        initial_value=initial_value,
        name=f"{variable_name}/{model_variable._shared_name}",  # pylint: disable=protected-access
        dtype=model_variable.dtype,
        trainable=False)

  def minimize(self, loss, var_list, tape=None):
    """Minimize `loss` by updating `var_list`.

    This method simply computes gradient using `tf.GradientTape` and calls
    `apply_gradients()`. If you want to process the gradient before applying
    then call `tf.GradientTape` and `apply_gradients()` explicitly instead
    of using this function.

    Args:
      loss: `Tensor` or callable. If a callable, `loss` should take no arguments
        and return the value to minimize. If a `Tensor`, the `tape` argument
        must be passed.
      var_list: list or tuple of `Variable` objects to update to minimize
        `loss`.
      tape: (Optional) `tf.GradientTape`.

    Returns:
      None
    """
    grads, var_list = self._compute_gradients(loss, var_list, tape)
    self.apply_gradients(zip(grads, var_list))

  def apply_gradients(self, grads_and_vars):
    """Apply gradients to variables.

    Args:
      grads_and_vars: List of (gradient, variable) pairs.

    Returns:
      None

    Raises:
      TypeError: If `grads_and_vars` is malformed.
    """
    grads, trainable_variables = zip(*grads_and_vars)
    scope_name = self._name or "optimizer"
    with tf.name_scope(scope_name):
      with tf.init_scope():
        # Lift variable creation to init scope to avoid enviroment issues.
        self.build(trainable_variables)
    grads = self._clip_gradients(grads)
    grads_and_vars = list(zip(grads, trainable_variables))
    self._internal_apply_gradients(grads_and_vars)

  def _internal_apply_gradients(self, grads_and_vars):
    """Helper function of apply gradients.

    This is required for separating out distributed training logic.

    Args:
      grads_and_vars: List of (gradient, variable) pairs.
    """
    for grad, var in grads_and_vars:
      self.update_step(grad, var)
    self.iterations.assign_add(1)

  def _serialize_hyperparameter(self, hyperparameter):
    """Serialize a hyperparameter that can be a numeric or callable."""
    if isinstance(hyperparameter, learning_rate_schedule.LearningRateSchedule):
      return learning_rate_schedule.serialize(hyperparameter)
    if isinstance(hyperparameter, tf.Variable):
      return hyperparameter.numpy()
    if callable(hyperparameter):
      return hyperparameter()
    return hyperparameter

  def get_config(self):
    """Returns the config of the optimizer.

    An optimizer config is a Python dictionary (serializable)
    containing the configuration of an optimizer.
    The same optimizer can be reinstantiated later
    (without any saved state) from this configuration.

    Subclass optimizer should override this method to include other
    hyperparameters.

    Returns:
        Python dictionary.
    """
    config = {}
    if hasattr(self, "_clipnorm"):
      config["clipnorm"] = self._clipnorm
    if hasattr(self, "_global_clipnorm"):
      config["clipnorm"] = self._global_clipnorm
    if hasattr(self, "_clipvalue"):
      config["clipvalue"] = self._clipvalue
    return config

  @classmethod
  def from_config(cls, config):
    """Creates an optimizer from its config.

    This method is the reverse of `get_config`, capable of instantiating the
    same optimizer from the config dictionary.

    Args:
        config: A Python dictionary, typically the output of get_config.

    Returns:
        An optimizer instance.
    """
    if "learning_rate" in config:
      if isinstance(config["learning_rate"], dict):
        config["learning_rate"] = learning_rate_schedule.deserialize(
            config["learning_rate"])
    return cls(**config)


class Optimizer(_BaseOptimizer):
  """Abstract optimizer base class.

  This class supports distributed training. If you want to implement your own
  optimizer, please subclass this class instead of _BaseOptimizer.
  """

  def __init__(self, name, clipnorm=None, clipvalue=None, global_clipnorm=None):
    """Create a new Optimizer.

    Args:
      name: String. The name to use for momentum accumulator weights created by
        the optimizer.
      clipnorm: float. If set, the gradient of each weight is individually
        clipped so that its norm is no higher than this value.
      clipvalue: float. If set, the gradient of each weight is clipped to be
        no higher than this value.
      global_clipnorm: float. If set, the gradient of all weights is clipped
        so that their global norm is no higher than this value.
    """
    super().__init__(name, clipnorm, clipvalue, global_clipnorm)
    self._distribution_strategy = tf.distribute.get_strategy()

  def add_variable_from_reference(self,
                                  model_variable,
                                  variable_name,
                                  initial_value=None):
    """Create an optimizer variable.

    Create an optimizer variable based on the information of model variable.
    The created optimizer variable will have the same shape and dtype as the
    model variable, and placed at the same device.

    Args:
      model_variable: The corresponding model variable to the optimizer variable
        to be created.
      variable_name: The name prefix of the optimizer variable to be created.
      initial_value: The initial value of the optimizer variable, if None, the
        value will be default to 0.

    Returns:
      An optimizer variable.
    """
    strategy = tf.distribute.get_strategy()
    with strategy.extended.colocate_vars_with(model_variable):
      return super(Optimizer,
                   self).add_variable_from_reference(model_variable,
                                                     variable_name,
                                                     initial_value)

  def _var_key(self, variable):
    """Get a unique identifier of the given variable."""
    # pylint: disable=protected-access
    # Get the distributed variable if it exists.
    # TODO(b/197554203): replace _distributed_container() with a public api.
    if hasattr(variable, "_distributed_container"):
      variable = variable._distributed_container()
    return super(Optimizer, self)._var_key(variable)

  def _aggregate_gradients(self, grads_and_vars):
    return optimizer_utils.all_reduce_sum_gradients(grads_and_vars)

  def apply_gradients(self, grads_and_vars, skip_gradients_aggregation=False):
    """Apply gradients to variables.

    Args:
      grads_and_vars: List of (gradient, variable) pairs.
      skip_gradients_aggregation: If true, gradients aggregation will not be
        performed inside optimizer. Usually this arg is set to True when you
        write custom code aggregating gradients outside the optimizer.

    Returns:
      None

    Raises:
      TypeError: If `grads_and_vars` is malformed.
      RuntimeError: If called in a cross-replica context.
    """
    if not skip_gradients_aggregation:
      grads_and_vars = self._aggregate_gradients(grads_and_vars)
    super().apply_gradients(grads_and_vars)

  def _internal_apply_gradients(self, grads_and_vars):
    # TODO(b/202332404): create a tf.distribute util to handle the if-else.
    if optimizer_utils.strategy_supports_no_merge_call():
      self._distributed_apply_gradients(self._distribution_strategy,
                                        grads_and_vars)
    else:
      tf.distribute.get_replica_context().merge_call(
          functools.partial(self._distributed_apply_gradients),
          args=(grads_and_vars,))

  def _distributed_apply_gradients(self, distribution, grads_and_vars):
    """`apply_gradients` using a `DistributionStrategy`."""

    def apply_grad_to_update_var(var, grad):
      return self.update_step(grad, var)

    for grad, var in grads_and_vars:
      distribution.extended.update(
          var, apply_grad_to_update_var, args=(grad,), group=False)
    self.iterations.assign_add(1)


class RestoredOptimizer(Optimizer):

  def __init__(self):
    super(RestoredOptimizer, self).__init__("RestoredOptimizer")

  def get_config(self):
    raise NotImplementedError(
        "Restoring functional Optimizers from SavedModels is not currently "
        "supported. Please file a feature request if this limitation bothers "
        "you.")


# Register the optimizer for loading from saved_model purpose.
tf.__internal__.saved_model.load.register_revived_type(
    "optimizerV3",
    lambda obj: isinstance(obj, Optimizer),
    versions=[
        tf.__internal__.saved_model.load.VersionedTypeRegistration(
            object_factory=lambda proto: RestoredOptimizer(),
            version=2,
            min_producer_version=1,
            min_consumer_version=1)
    ])
