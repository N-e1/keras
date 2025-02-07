# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
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
"""Tests for Keras metrics."""

import tensorflow.compat.v2 as tf

from absl.testing import parameterized
from keras import metrics
from keras.engine import base_layer


def _labeled_dataset_fn():
  # First four batches of x: labels, predictions -> (labels == predictions)
  #  0: 0, 0 -> True;   1: 1, 1 -> True;   2: 2, 2 -> True;   3: 3, 0 -> False
  #  4: 4, 1 -> False;  5: 0, 2 -> False;  6: 1, 0 -> False;  7: 2, 1 -> False
  #  8: 3, 2 -> False;  9: 4, 0 -> False; 10: 0, 1 -> False; 11: 1, 2 -> False
  # 12: 2, 0 -> False; 13: 3, 1 -> False; 14: 4, 2 -> False; 15: 0, 0 -> True
  return tf.data.Dataset.range(1000).map(
      lambda x: {"labels": x % 5, "predictions": x % 3}).batch(
          4, drop_remainder=True)


def _boolean_dataset_fn():
  # First four batches of labels, predictions: {TP, FP, TN, FN}
  # with a threshold of 0.5:
  #   T, T -> TP;  F, T -> FP;   T, F -> FN
  #   F, F -> TN;  T, T -> TP;   F, T -> FP
  #   T, F -> FN;  F, F -> TN;   T, T -> TP
  #   F, T -> FP;  T, F -> FN;   F, F -> TN
  return tf.data.Dataset.from_tensor_slices({
      "labels": [True, False, True, False],
      "predictions": [True, True, False, False]}).repeat().batch(
          3, drop_remainder=True)


def _threshold_dataset_fn():
  # First four batches of labels, predictions: {TP, FP, TN, FN}
  # with a threshold of 0.5:
  #   True, 1.0 -> TP;  False, .75 -> FP;   True, .25 -> FN
  #  False, 0.0 -> TN;   True, 1.0 -> TP;  False, .75 -> FP
  #   True, .25 -> FN;  False, 0.0 -> TN;   True, 1.0 -> TP
  #  False, .75 -> FP;   True, .25 -> FN;  False, 0.0 -> TN
  return tf.data.Dataset.from_tensor_slices({
      "labels": [True, False, True, False],
      "predictions": [1.0, 0.75, 0.25, 0.]}).repeat().batch(
          3, drop_remainder=True)


def _regression_dataset_fn():
  return tf.data.Dataset.from_tensor_slices({
      "labels": [1., .5, 1., 0.],
      "predictions": [1., .75, .25, 0.]}).repeat()


def all_combinations():
  return tf.__internal__.test.combinations.combine(
      distribution=[
          tf.__internal__.distribute.combinations.default_strategy,
          tf.__internal__.distribute.combinations.one_device_strategy,
          tf.__internal__.distribute.combinations.mirrored_strategy_with_gpu_and_cpu,
          tf.__internal__.distribute.combinations.mirrored_strategy_with_two_gpus
      ],
      mode=["graph"])


def tpu_combinations():
  return tf.__internal__.test.combinations.combine(
      distribution=[tf.__internal__.distribute.combinations.tpu_strategy,],
      mode=["graph"])


class KerasMetricsTest(tf.test.TestCase, parameterized.TestCase):

  def _test_metric(self, distribution, dataset_fn, metric_init_fn, expected_fn):
    with tf.Graph().as_default(), distribution.scope():
      metric = metric_init_fn()

      iterator = distribution.make_input_fn_iterator(lambda _: dataset_fn())
      updates = distribution.experimental_local_results(
          distribution.run(metric, args=(iterator.get_next(),)))
      batches_per_update = distribution.num_replicas_in_sync

      self.evaluate(iterator.initializer)
      self.evaluate([v.initializer for v in metric.variables])

      batches_consumed = 0
      for i in range(4):
        batches_consumed += batches_per_update
        self.evaluate(updates)
        self.assertAllClose(expected_fn(batches_consumed),
                            self.evaluate(metric.result()),
                            0.001,
                            msg="After update #" + str(i+1))
        if batches_consumed >= 4:  # Consume 4 input batches in total.
          break

  @tf.__internal__.distribute.combinations.generate(all_combinations() + tpu_combinations())
  def testMean(self, distribution):
    def _dataset_fn():
      return tf.data.Dataset.range(1000).map(tf.compat.v1.to_float).batch(
          4, drop_remainder=True)

    def _expected_fn(num_batches):
      # Mean(0..3) = 1.5, Mean(0..7) = 3.5, Mean(0..11) = 5.5, etc.
      return num_batches * 2 - 0.5

    self._test_metric(distribution, _dataset_fn, metrics.Mean, _expected_fn)

  @tf.__internal__.distribute.combinations.generate(
      tf.__internal__.test.combinations.combine(
          distribution=[
              tf.__internal__.distribute.combinations.mirrored_strategy_with_one_cpu,
              tf.__internal__.distribute.combinations.mirrored_strategy_with_gpu_and_cpu,
              tf.__internal__.distribute.combinations.mirrored_strategy_with_two_gpus,
              tf.__internal__.distribute.combinations.tpu_strategy_packed_var
          ],
          mode=["eager"],
          jit_compile=[False]) +
      tf.__internal__.test.combinations.combine(
          distribution=[tf.__internal__.distribute.combinations.mirrored_strategy_with_two_gpus],
          mode=["eager"],
          jit_compile=[True]))
  def testAddMetric(self, distribution, jit_compile):
    if not tf.__internal__.tf2.enabled():
      self.skipTest("Skip test since tf2 is not enabled. Pass "
                    " --test_env=TF2_BEHAVIOR=1 to enable tf2 behavior.")

    class MetricLayer(base_layer.Layer):

      def __init__(self):
        super(MetricLayer, self).__init__(name="metric_layer")
        self.sum = metrics.Sum(name="sum")
        self.sum_var = tf.Variable(1.0)

      def call(self, inputs):
        self.add_metric(self.sum(inputs))
        self.add_metric(
            tf.reduce_mean(inputs), name="mean", aggregation="mean")
        self.sum_var.assign(self.sum.result())
        return inputs

    with distribution.scope():
      layer = MetricLayer()

    def func():
      return layer(tf.ones(()))

    if jit_compile:
      func = tf.function(jit_compile=True)(func)

    @tf.function
    def run():
      return distribution.run(func)

    run()

    self.assertEqual(layer.metrics[0].result().numpy(),
                     1.0 * distribution.num_replicas_in_sync)
    self.assertEqual(layer.metrics[1].result().numpy(), 1.0)
    self.assertEqual(layer.sum_var.read_value().numpy(),
                     1.0 * distribution.num_replicas_in_sync)


if __name__ == "__main__":
  tf.test.main()
