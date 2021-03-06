# Copyright 2017 Natural Language Processing Group, Nanjing University, zhaocq.nlp@gmail.com.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" The functions for building a model. """
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
from collections import namedtuple

import njunmt
from njunmt.models.ensemble_model import EnsembleModel
from njunmt.utils.configurable import ModelConfigs
from njunmt.utils.global_names import GlobalNames
from njunmt.training.optimize import optimize
from njunmt.training.hooks import build_hooks


class EstimatorSpec(
    namedtuple('EstimatorSpec', [
        'predictions', 'loss', 'train_op',
        'training_chief_hooks', 'training_hooks'])):
    """ Defines a collection of operations and objects
    returned by `model_fn`.

    Refer to tf.estimator.EstimatorSpec.
    """

    def __new__(cls,
                mode,
                predictions=None,
                loss=None,
                train_op=None,
                training_chief_hooks=None,
                training_hooks=None):
        """ Creates a validated `EstimatorSpec` instance.

        Depending on the value of `mode`, different arguments are required. Namely
        * For `mode == ModeKeys.TRAIN`: required fields are `loss` and `train_op`.
        * For `mode == ModeKeys.EVAL`: required field is`loss`.
        * For `mode == ModeKeys.PREDICT`: required fields are `predictions`.

        Args:
            mode: A `ModeKeys`. Specifies if this is training, evaluation or
              inference.
            predictions: A dict of Tensor for inference.
            loss: Training loss Tensor. Must be either scalar, or with shape `[1]`.
            train_op: Op for the training step.
            training_chief_hooks: Iterable of `tf.train.SessionRunHook` objects to
              run on the chief worker during training.
            training_hooks: Iterable of `tf.train.SessionRunHook` objects that to run
              on all workers during training.

        Returns: A validated `EstimatorSpec` object.

        Raises:
            ValueError: If validation fails.
            TypeError: If any of the arguments is not the expected type.
        """
        if predictions is None and mode == tf.contrib.learn.ModeKeys.INFER:
            raise ValueError("Missing predictions")
        if loss is None:
            if mode in (tf.contrib.learn.ModeKeys.TRAIN,
                        tf.contrib.learn.ModeKeys.EVAL):
                raise ValueError("Missing loss.")
        if train_op is None and mode == tf.contrib.learn.ModeKeys.TRAIN:
            raise ValueError("Missing train_op.")

        training_chief_hooks = tuple(training_chief_hooks or [])
        training_hooks = tuple(training_hooks or [])
        for hook in training_hooks + training_chief_hooks:
            if not isinstance(hook, tf.train.SessionRunHook):
                raise TypeError(
                    'All hooks must be SessionRunHook instances, given: {}'.format(
                        hook))
        return super(EstimatorSpec, cls).__new__(
            cls,
            predictions=predictions,
            loss=loss,
            train_op=train_op,
            training_chief_hooks=training_chief_hooks,
            training_hooks=training_hooks)


def _inspect_varname_prefix(var_name):
    """ Returns the top variable scope name. """
    # empirical
    keywords = "/input_symbol_modality"
    if keywords in var_name:
        return var_name[:var_name.index(keywords)]
    keywords = "/symbol_modality_"
    if keywords in var_name:
        return var_name[:var_name.index(keywords)]
    return None


def model_fn(
        model_configs,
        mode,
        dataset,
        name=None,
        reuse=None,
        distributed_mode=False,
        is_chief=True,
        verbose=True):
    """ Creates NMT model for training, evaluation or inference.

    Args:
        model_configs: A dictionary of all configurations.
        mode: A mode.
        dataset: A `Dataset` object.
        name: A string, the name of top-level of the variable scope.
        reuse: Whether to reuse all variables, the parameter passed
          to `tf.variable_scope()`.
        verbose: Print model parameters if set True.
        distributed_mode: Whether training is on distributed mode.
        is_chief: Whether is the chief worker.

    Returns: A `EstimatorSpec` object.
    """
    # Create model template function
    model_name = name or model_configs["model"].split(".")[-1]
    if verbose:
        tf.logging.info("Create model: {} for {}".format(
            model_configs["model"], mode))
    model = eval(model_configs["model"])(
        params=model_configs["model_params"],
        mode=mode,
        vocab_source=dataset.vocab_source,
        vocab_target=dataset.vocab_target,
        name=model_name,
        verbose=verbose)
    # model_template_builder = tf.make_template("", model.build, create_scope_now_=False)
    # model_output = model_template_builder(dataset.input_fields)
    with tf.variable_scope("", reuse=reuse):
        model_output = model.build(dataset.input_fields)
    # training mode
    if mode == tf.contrib.learn.ModeKeys.TRAIN:
        loss = model_output[0]
        # Register the training loss in a collection so that hooks can easily fetch them
        tf.add_to_collection(GlobalNames.DISPLAY_KEY_COLLECTION_NAME, GlobalNames.TRAIN_LOSS_KEY_NAME)
        tf.add_to_collection(GlobalNames.DISPLAY_VALUE_COLLECTION_NAME, loss)
        # build train op
        train_op = optimize(loss, model_configs["optimizer_params"])
        # build training hooks
        hooks = build_hooks(model_configs, distributed_mode=distributed_mode, is_chief=is_chief)
        from njunmt.training.text_metrics_spec import build_eval_metrics
        hooks.extend(build_eval_metrics(model_configs, dataset,
                                        is_cheif=is_chief, model_name=model_name))
        return EstimatorSpec(
            mode,
            loss=loss,
            train_op=train_op,
            training_hooks=hooks,
            training_chief_hooks=None)

    # evaluation mode
    if mode == tf.contrib.learn.ModeKeys.EVAL:
        loss = model_output[0]
        return EstimatorSpec(
            mode,
            loss=loss)

    assert mode == tf.contrib.learn.ModeKeys.INFER
    predictions = model_output
    return EstimatorSpec(
        mode,
        predictions=predictions)


def model_fn_ensemble(
        model_dirs,
        dataset,
        weight_scheme,
        inference_options,
        verbose=True):
    """ Reloads NMT models from checkpoints and builds the ensemble
    model inference.

    Args:
        model_dirs: A list of model directories (checkpoints).
        dataset: A `Dataset` object.
        weight_scheme: A string, the ensemble weights. See
          `EnsembleModel.get_ensemble_weights()` for more details.
        inference_options: Contains beam_size, length_penalty and
          maximum_labels_length.
        verbose: Print logging info if set True.

    Returns: A `EstimatorSpec` object.
    """

    # load variable, rename (add prefix to varname), build model
    models = []
    for index, model_dir in enumerate(model_dirs):
        if verbose:
            tf.logging.info("loading variables from {}".format(model_dir))
        # load variables
        model_name = None
        for var_name, _ in tf.contrib.framework.list_variables(model_dir):
            if var_name.startswith("OptimizeLoss"):
                continue
            if model_name is None:
                model_name = _inspect_varname_prefix(var_name)
            var = tf.contrib.framework.load_variable(model_dir, var_name)
            with tf.variable_scope(GlobalNames.ENSEMBLE_VARNAME_PREFIX + str(index)):
                var = tf.get_variable(
                    name=var_name, shape=var.shape, dtype=tf.float32,
                    initializer=tf.constant_initializer(var))
        # load model configs
        assert model_name, (
            "Fail to fetch model name")
        model_configs = ModelConfigs.load(model_dir)
        if verbose:
            tf.logging.info("Create model: {}.".format(
                model_configs["model"]))
        model = eval(model_configs["model"])(
            params=model_configs["model_params"],
            mode=tf.contrib.learn.ModeKeys.INFER,
            vocab_source=dataset.vocab_source,
            vocab_target=dataset.vocab_target,
            name=model_name,
            verbose=False)
        models.append(model)
    ensemble_model = EnsembleModel(
        weight_scheme=weight_scheme,
        inference_options=inference_options)
    with tf.variable_scope("", reuse=True):
        predictions = ensemble_model.build(
            input_fields=dataset.input_fields, base_models=models,
            vocab_target=dataset.vocab_target)
    return EstimatorSpec(
        tf.contrib.learn.ModeKeys.INFER,
        predictions=predictions)
