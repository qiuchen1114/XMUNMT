#!/usr/bin/env python
# coding=utf-8
# Copyright 2017 Natural Language Processing Lab of Xiamen University
# Author: Zhixing Tan
# Contact: playinf@stu.xmu.edu.cn

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import argparse
import numpy as np
import tensorflow as tf

import xmunmt.models as models
import xmunmt.data.record as record
import xmunmt.utils.search as search
import xmunmt.data.dataset as dataset
import xmunmt.data.vocab as vocabulary
import xmunmt.utils.parallel as parallel
import xmunmt.utils.hooks as hooks


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description="Training neural machine translation models",
        usage="trainer.py [<args>] [-h | --help]"
    )

    # input files
    parser.add_argument("--input", type=str, nargs=2,
                        help="Path of source and target corpus")
    parser.add_argument("--record", type=str,
                        help="Path to tf.Record data")
    parser.add_argument("--output", type=str, default="train",
                        help="Path to saved models")
    parser.add_argument("--vocabulary", type=str, nargs=2,
                        help="Path of source and target vocabulary")
    parser.add_argument("--validation", type=str,
                        help="Path of validation file")
    parser.add_argument("--references", type=str, nargs="+",
                        help="Path of reference files")

    # model and configuration
    parser.add_argument("--model", type=str, required=True,
                        help="Name of the model")
    parser.add_argument("--parameters", type=str, default="",
                        help="Additional hyper parameters")

    return parser.parse_args(args)


def default_parameters():
    params = tf.contrib.training.HParams(
        input=["", ""],
        output="",
        record="",
        model="transformer",
        vocab=["", ""],
        # Default training hyper parameters
        num_threads=6,
        batch_size=128,
        max_length=256,
        length_multiplier=1,
        mantissa_bits=1,
        warmup_steps=4000,
        train_steps=100000,
        buffer_size=10000,
        constant_batch_size=True,
        device_list=[0],
        initializer="uniform",
        initializer_gain=0.08,
        adam_beta1=0.9,
        adam_beta2=0.999,
        adam_epsilon=1e-8,
        clip_grad_norm=5.0,
        learning_rate=1.0,
        learning_rate_decay="noam",
        learning_rate_boundaries=[0],
        learning_rate_values=[0.0],
        keep_checkpoint_max=20,
        keep_top_checkpoint_max=5,
        # Validation
        eval_steps=2000,
        eval_secs=0,
        eval_batch_size=32,
        top_beams=1,
        beam_size=4,
        decode_alpha=0.6,
        decode_length=50,
        decode_constant=5.0,
        decode_normalize=False,
        validation="",
        references=[""],
        save_checkpoint_secs=0,
        save_checkpoint_steps=1000
    )

    return params


def import_params(model_dir, model_name, params):
    model_dir = os.path.abspath(model_dir)
    p_name = os.path.join(model_dir, "params.json")
    m_name = os.path.join(model_dir, model_name + ".json")

    if not tf.gfile.Exists(p_name) or not tf.gfile.Exists(m_name):
        return params

    with tf.gfile.Open(p_name) as fd:
        tf.logging.info("Restoring hyper parameters from %s" % p_name)
        json_str = fd.readline()
        params.parse_json(json_str)

    with tf.gfile.Open(m_name) as fd:
        tf.logging.info("Restoring model parameters from %s" % m_name)
        json_str = fd.readline()
        params.parse_json(json_str)

    return params


def export_params(output_dir, name, params):
    if not tf.gfile.Exists(output_dir):
        tf.gfile.MkDir(output_dir)

    # Save params as params.json
    filename = os.path.join(output_dir, name)
    with tf.gfile.Open(filename, "w") as fd:
        fd.write(params.to_json())


def collect_params(all_params, params):
    collected = tf.contrib.training.HParams()

    for k in params.values().iterkeys():
        collected.add_hparam(k, getattr(all_params, k))

    return collected


def merge_parameters(params1, params2):
    params = tf.contrib.training.HParams()

    for (k, v) in params1.values().iteritems():
        params.add_hparam(k, v)

    params_dict = params.values()

    for (k, v) in params2.values().iteritems():
        if k in params_dict:
            # Override
            setattr(params, k, v)
        else:
            params.add_hparam(k, v)

    return params


def override_parameters(params, args):
    params.model = args.model
    params.input = args.input or params.input
    params.output = args.output or params.output
    params.record = args.record or params.record
    params.vocab = args.vocabulary or params.vocab
    params.validation = args.validation or params.validation
    params.references = args.references or params.references
    params.parse(args.parameters)

    params.vocabulary = {
        "source": vocabulary.load_vocabulary(params.vocab[0]),
        "target": vocabulary.load_vocabulary(params.vocab[1])
    }
    params.vocabulary["source"] = vocabulary.process_vocabulary(
        params.vocabulary["source"], params
    )
    params.vocabulary["target"] = vocabulary.process_vocabulary(
        params.vocabulary["target"], params
    )

    control_symbols = [params.pad, params.bos, params.eos, params.unk]

    params.mapping = {
        "source": vocabulary.get_control_mapping(
            params.vocabulary["source"],
            control_symbols
        ),
        "target": vocabulary.get_control_mapping(
            params.vocabulary["target"],
            control_symbols
        )
    }

    return params


def get_initializer(params):
    if params.initializer == "uniform":
        max_val = params.initializer_gain
        return tf.random_uniform_initializer(-max_val, max_val)
    elif params.initializer == "normal":
        return tf.random_normal_initializer(0.0, params.initializer_gain)
    elif params.initializer == "normal_unit_scaling":
        return tf.variance_scaling_initializer(params.initializer_gain,
                                               mode="fan_avg",
                                               distribution="normal")
    elif params.initializer == "uniform_unit_scaling":
        return tf.variance_scaling_initializer(params.initializer_gain,
                                               mode="fan_avg",
                                               distribution="uniform")
    else:
        raise ValueError("Unrecognized initializer: %s" % params.initializer)


def get_learning_rate_decay(learning_rate, global_step, params):
    if params.learning_rate_decay == "noam":
        step = tf.to_float(global_step)
        warmup_steps = tf.to_float(params.warmup_steps)
        multiplier = params.hidden_size ** -0.5
        decay = multiplier * tf.minimum((step + 1) * (warmup_steps ** -1.5),
                                        (step + 1) ** -0.5)

        return learning_rate * decay
    elif params.learning_rate_decay == "piecewise_constant":
        return tf.train.piecewise_constant(tf.to_int32(global_step),
                                           params.learning_rate_boundaries,
                                           params.learning_rate_values)
    elif params.learning_rate_decay == "none":
        return learning_rate
    else:
        raise ValueError("Unknown learning_rate_decay")


def session_config(params):
    optimizer_options = tf.OptimizerOptions(opt_level=tf.OptimizerOptions.L1,
                                            do_function_inlining=True)
    graph_options = tf.GraphOptions(optimizer_options=optimizer_options)
    config = tf.ConfigProto(allow_soft_placement=True,
                            graph_options=graph_options)
    if params.device_list:
        device_str = ",".join([str(i) for i in params.device_list])
        config.gpu_options.visible_device_list = device_str

    return config


def decode_target_ids(inputs, params):
    decoded = []
    vocab = params.vocabulary["target"]

    for item in inputs:
        syms = []
        for idx in item:
            sym = vocab[idx]

            if sym == params.eos:
                break

            if sym == params.pad:
                break

            syms.append(sym)
        decoded.append(syms)

    return decoded


def main(args):
    tf.logging.set_verbosity(tf.logging.INFO)
    model_cls = models.get_model(args.model)
    params = default_parameters()

    # Import and override parameters
    # Priorities (low -> high):
    # default -> saved -> command
    params = merge_parameters(params, model_cls.get_parameters())
    params = import_params(args.output, args.model, params)
    override_parameters(params, args)

    # Export all parameters and model specific parameters
    export_params(params.output, "params.json", params)
    export_params(
        params.output,
        "%s.json" % args.model,
        collect_params(params, model_cls.get_parameters())
    )

    # Build Graph
    with tf.Graph().as_default():
        if not params.record:
            # Build input queue
            features = dataset.get_training_input(params.input, params)
        else:
            features = record.get_input_features(
                os.path.join(params.record, "*train*"), "train", params
            )

        # Build model
        initializer = get_initializer(params)
        model = model_cls(params)

        # Multi-GPU setting
        sharded_losses = parallel.parallel_model(
            model.get_training_func(initializer),
            features,
            params.device_list
        )
        loss = tf.add_n(sharded_losses) / len(sharded_losses)

        # Create global step
        global_step = tf.train.get_or_create_global_step()

        # Print parameters
        all_weights = {v.name: v for v in tf.trainable_variables()}
        total_size = 0

        for v_name in sorted(list(all_weights)):
            v = all_weights[v_name]
            tf.logging.info("%s\tshape    %s", v.name[:-2].ljust(80),
                            str(v.shape).ljust(20))
            v_size = np.prod(np.array(v.shape.as_list())).tolist()
            total_size += v_size
        tf.logging.info("Total trainable variables size: %d", total_size)

        learning_rate = get_learning_rate_decay(params.learning_rate,
                                                global_step, params)
        tf.summary.scalar("learning_rate", learning_rate)

        # Create optimizer
        opt = tf.train.AdamOptimizer(learning_rate,
                                     beta1=params.adam_beta1,
                                     beta2=params.adam_beta2,
                                     epsilon=params.adam_epsilon)

        train_op = tf.contrib.layers.optimize_loss(
            name="training",
            loss=loss,
            global_step=global_step,
            learning_rate=learning_rate,
            clip_gradients=params.clip_grad_norm or None,
            optimizer=opt,
            colocate_gradients_with_ops=True
        )

        # Validation
        if params.validation and params.references[0]:
            files = [params.validation] + list(params.references)
            eval_inputs = dataset.sort_and_zip_files(files)
            eval_input_fn = dataset.get_evaluation_input
        else:
            eval_input_fn = None

        # Add hooks
        train_hooks = [
            tf.train.StopAtStepHook(last_step=params.train_steps),
            tf.train.NanTensorHook(loss),
            tf.train.LoggingTensorHook(
                {
                    "step": global_step,
                    "loss": loss,
                    "source": tf.shape(features["source"]),
                    "target": tf.shape(features["target"])
                },
                every_n_iter=1
            ),
            tf.train.CheckpointSaverHook(
                checkpoint_dir=params.output,
                save_secs=params.save_checkpoint_secs or None,
                save_steps=params.save_checkpoint_steps or None,
                saver=tf.train.Saver(
                    max_to_keep=params.keep_checkpoint_max,
                    sharded=False
                )
            )
        ]

        config = session_config(params)

        if eval_input_fn is not None:
            train_hooks.append(
                hooks.EvaluationHook(
                    lambda f: search.create_inference_graph(
                        model.get_evaluation_func(), f, params
                    ),
                    lambda: eval_input_fn(eval_inputs, params),
                    lambda x: decode_target_ids(x, params),
                    params.output,
                    config,
                    params.keep_top_checkpoint_max,
                    eval_secs=params.eval_secs,
                    eval_steps=params.eval_steps
                )
            )

        # Create session, do not use default CheckpointSaverHook
        with tf.train.MonitoredTrainingSession(
                checkpoint_dir=params.output, hooks=train_hooks,
                save_checkpoint_secs=None, config=config) as sess:
            while not sess.should_stop():
                sess.run(train_op)


if __name__ == "__main__":
    main(parse_args())
