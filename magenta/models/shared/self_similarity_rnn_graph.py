# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Provides function to build an self-similarity RNN model's graph."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# internal imports
import numpy as np
import six
import tensorflow as tf
import magenta

from tensorflow.python.util import nest as tf_nest


def make_rnn_cell(rnn_layer_sizes,
                  base_cell=tf.contrib.rnn.BasicLSTMCell):
  """Makes a RNN cell from the given hyperparameters.

  Args:
    rnn_layer_sizes: A list of integer sizes (in units) for each layer of the
        RNN.
    base_cell: The base tf.contrib.rnn.RNNCell to use for sub-cells.

  Returns:
      A tf.contrib.rnn.MultiRNNCell based on the given hyperparameters.
  """
  cells = []
  for num_units in rnn_layer_sizes:
    cell = base_cell(num_units)
    cells.append(cell)

  cell = tf.contrib.rnn.MultiRNNCell(cells)

  return cell


def input_embeddings(inputs, input_size, embedding_size):
  """Computes embeddings for a batch of input sequences.

  These embeddings are used to compute self-similarity over the input sequences.

  Args:
    inputs: A tensor of input sequences with shape
        `[batch_size, num_steps, input_size]`.
    input_size: The size of each input vector.
    embedding_size: The size of the output embedding.

  Returns:
    A tensor with shape `[batch_size, num_steps, embedding_size]` containing the
    input embedding at each step.
  """
  inputs_flat = tf.expand_dims(inputs, -1)

  # This isn't really a 2D convolution, but a fully-connected layer operating on
  # each input step independently.
  embeddings = tf.contrib.layers.conv2d(
      inputs_flat, embedding_size, [1, input_size], padding='VALID',
      activation_fn=tf.nn.relu)

  return tf.squeeze(embeddings, axis=2)


def similarity_weighted_attention(targets, self_similarity):
  """Computes similarity-weighted softmax attention over target vectors.

  For each step, computes an attention-weighted sum of the target vectors,
  where attention is determined by self-similarity.

  Args:
    targets: A tensor of target vector sequences with shape
        `[batch_size, num_target_steps, target_size]`.
    self_similarity: A tensor of input self-similarities based on embeddings,
        with shape `[batch_size, num_input_steps, num_target_steps]`.

  Returns:
    A tensor with shape `[batch_size, num_input_steps, target_size]` containing
    the similarity-weighted attention over targets for each step.
  """
  num_input_steps = tf.shape(self_similarity)[1]
  num_target_steps = tf.shape(self_similarity)[2]

  steps = tf.range(num_target_steps - num_input_steps + 1, num_target_steps + 1)
  transposed_self_similarity = tf.transpose(self_similarity, [1, 0, 2])

  # This computes a masked softmax to prevent attending to the future.
  def similarity_to_attention(enumerated_similarity):
    step, sim = enumerated_similarity
    return tf.concat(
        [tf.nn.softmax(sim[:, :step]), tf.zeros_like(sim[:, step:])], axis=-1)

  transposed_attention = tf.map_fn(
      similarity_to_attention, (steps, transposed_self_similarity),
      dtype=tf.float32)
  attention = tf.transpose(transposed_attention, [1, 0, 2])

  return tf.matmul(attention, targets)


def self_similarity_attention(inputs, past_inputs, batch_size, input_size,
                              embedding_size):
  """Computes self-similarity attention over inputs via embeddings.

  Args:
    inputs: A tensor of input sequences with shape
        `[batch_size, num_steps, input_size]`.
    past_inputs: A tensor of past inputs with shape
        `[batch_size, num_past_steps, input_size]`. Can be empty.
    batch_size: The number of sequences per batch.
    input_size: The size of each input vector.
    embedding_size: The size of the embedding used to compute similarity.

  Returns:
    attention_outputs: A softmax-weighted sum of "targets" consisting of all
        past and current inputs, a tensor with shape
        `[batch_size, num_steps, input_size]`.
    self_similarity: The self-similarity matrix used to compute attention, a
        tensor with shape `[batch_size, num_steps, num_targets]`.
  """
  embeddings = input_embeddings(inputs, input_size, embedding_size)

  targets = tf.concat([past_inputs, inputs], axis=1)
  target_embeddings = input_embeddings(
      targets[:, :-1, :], input_size, embedding_size)

  # Compute similarity between current embeddings and embeddings for all targets
  # (except the last).
  self_similarity = tf.matmul(embeddings, target_embeddings, transpose_b=True)

  # Compute similarity-weighted attention over all targets (except the first).
  attention_outputs = similarity_weighted_attention(
      targets[:, 1:, :], self_similarity)

  return attention_outputs, self_similarity


def build_graph(mode, config, sequence_example_file_paths=None):
  """Builds the TensorFlow graph.

  Args:
    mode: 'train', 'eval', or 'generate'. Only mode related ops are added to
        the graph.
    config: An EventSequenceRnnConfig containing the encoder/decoder and HParams
        to use.
    sequence_example_file_paths: A list of paths to TFRecord files containing
        tf.train.SequenceExample protos. Only needed for training and
        evaluation.

  Returns:
    A tf.Graph instance which contains the TF ops.

  Raises:
    ValueError: If mode is not 'train', 'eval', or 'generate'.
  """
  if mode not in ('train', 'eval', 'generate'):
    raise ValueError("The mode parameter must be 'train', 'eval', "
                     "or 'generate'. The mode parameter was: %s" % mode)

  hparams = config.hparams
  encoder_decoder = config.encoder_decoder

  tf.logging.info('hparams = %s', hparams.values())

  input_size = encoder_decoder.input_size
  num_classes = encoder_decoder.num_classes
  no_event_label = encoder_decoder.default_event_label

  num_layers = len(hparams.rnn_layer_sizes)

  with tf.Graph().as_default() as graph:
    inputs, labels, lengths = None, None, None
    past_targets = []

    if mode == 'train' or mode == 'eval':
      inputs, labels, lengths = magenta.common.get_padded_batch(
          sequence_example_file_paths, hparams.batch_size, input_size,
          shuffle=mode == 'train')
      for layer in range(num_layers):
        past_targets.append(tf.zeros(
            [hparams.batch_size, 0, hparams.rnn_layer_sizes[layer][-1]]))

    elif mode == 'generate':
      inputs = tf.placeholder(tf.float32, [hparams.batch_size, None,
                                           input_size])
      for layer in range(num_layers):
        past_targets.append(
            tf.placeholder(
                tf.float32,
                [hparams.batch_size, None, hparams.rnn_layer_sizes[layer][-1]]))

    targets = []
    initial_state = []
    final_state = []
    self_similarity = []

    layer_inputs = inputs

    for layer in range(num_layers):
      with tf.variable_scope('layer_%d' % (layer + 1)):
        # Each layer starts with an RNN.
        cell = make_rnn_cell(hparams.rnn_layer_sizes[layer])
        layer_initial_state = cell.zero_state(hparams.batch_size, tf.float32)
        rnn_outputs, layer_final_state = tf.nn.dynamic_rnn(
            cell, layer_inputs, sequence_length=lengths,
            initial_state=layer_initial_state, swap_memory=True)

        # Then the RNN output is run through a self-similarity attention layer.
        attention_outputs, layer_self_similarity = self_similarity_attention(
            rnn_outputs, past_targets[layer],
            batch_size=hparams.batch_size,
            input_size=hparams.rnn_layer_sizes[layer][-1],
            embedding_size=hparams.embedding_sizes[layer])

        # The final output is a concatenation of the RNN output and self-
        # similarity attention output.
        outputs = tf.concat([rnn_outputs, attention_outputs], axis=2)

        targets.append(rnn_outputs)
        initial_state.append(layer_initial_state)
        final_state.append(layer_final_state)
        self_similarity.append(layer_self_similarity)

        # Outputs are inputs to next layer.
        layer_inputs = outputs

    outputs_flat = magenta.common.flatten_maybe_padded_sequences(
        outputs, lengths)
    logits_flat = tf.contrib.layers.linear(outputs_flat, num_classes)

    if mode == 'train' or mode == 'eval':
      labels_flat = magenta.common.flatten_maybe_padded_sequences(
          labels, lengths)

      softmax_cross_entropy = tf.nn.sparse_softmax_cross_entropy_with_logits(
          labels=labels_flat, logits=logits_flat)

      predictions_flat = tf.argmax(logits_flat, axis=1)
      correct_predictions = tf.to_float(
          tf.equal(labels_flat, predictions_flat))
      event_positions = tf.to_float(tf.not_equal(labels_flat, no_event_label))
      no_event_positions = tf.to_float(tf.equal(labels_flat, no_event_label))

      # Compute the total number of time steps across all sequences in the
      # batch. For some models this will be different from the number of RNN
      # steps.
      def batch_labels_to_num_steps(batch_labels, lengths):
        num_steps = 0
        for labels, length in zip(batch_labels, lengths):
          num_steps += encoder_decoder.labels_to_num_steps(labels[:length])
        return np.float32(num_steps)
      num_steps = tf.py_func(
          batch_labels_to_num_steps, [labels, lengths], tf.float32)

      if mode == 'train':
        loss = tf.reduce_mean(softmax_cross_entropy)
        perplexity = tf.exp(loss)
        accuracy = tf.reduce_mean(correct_predictions)
        event_accuracy = (
            tf.reduce_sum(correct_predictions * event_positions) /
            tf.reduce_sum(event_positions))
        no_event_accuracy = (
            tf.reduce_sum(correct_predictions * no_event_positions) /
            tf.reduce_sum(no_event_positions))

        loss_per_step = tf.reduce_sum(softmax_cross_entropy) / num_steps
        perplexity_per_step = tf.exp(loss_per_step)

        optimizer = tf.train.AdamOptimizer(learning_rate=hparams.learning_rate)

        train_op = tf.contrib.slim.learning.create_train_op(
            loss, optimizer, clip_gradient_norm=hparams.clip_norm)
        tf.add_to_collection('train_op', train_op)

        vars_to_summarize = {
            'loss': loss,
            'metrics/perplexity': perplexity,
            'metrics/accuracy': accuracy,
            'metrics/event_accuracy': event_accuracy,
            'metrics/no_event_accuracy': no_event_accuracy,
            'metrics/loss_per_step': loss_per_step,
            'metrics/perplexity_per_step': perplexity_per_step,
        }

        # Make self-similarity image summaries for each layer.
        for layer in range(num_layers):
          tf.summary.image('self_similarity_%d' % (layer + 1),
                           self_similarity[layer], max_outputs=1)

      elif mode == 'eval':
        vars_to_summarize, update_ops = tf.contrib.metrics.aggregate_metric_map(
            {
                'loss': tf.metrics.mean(softmax_cross_entropy),
                'metrics/accuracy': tf.metrics.accuracy(
                    labels_flat, predictions_flat),
                'metrics/per_class_accuracy':
                    tf.metrics.mean_per_class_accuracy(
                        labels_flat, predictions_flat, num_classes),
                'metrics/event_accuracy': tf.metrics.recall(
                    event_positions, correct_predictions),
                'metrics/no_event_accuracy': tf.metrics.recall(
                    no_event_positions, correct_predictions),
                'metrics/loss_per_step': tf.metrics.mean(
                    softmax_cross_entropy,
                    weights=num_steps / tf.cast(tf.size(softmax_cross_entropy),
                                                tf.float32)),
            })
        for updates_op in update_ops.values():
          tf.add_to_collection('eval_ops', updates_op)

        # Perplexity is just exp(loss) and doesn't need its own update op.
        vars_to_summarize['metrics/perplexity'] = tf.exp(
            vars_to_summarize['loss'])
        vars_to_summarize['metrics/perplexity_per_step'] = tf.exp(
            vars_to_summarize['metrics/loss_per_step'])

      for var_name, var_value in six.iteritems(vars_to_summarize):
        tf.summary.scalar(var_name, var_value)
        tf.add_to_collection(var_name, var_value)

    elif mode == 'generate':
      temperature = tf.placeholder(tf.float32, [])
      softmax_flat = tf.nn.softmax(
          tf.div(logits_flat, tf.fill([num_classes], temperature)))
      softmax = tf.reshape(softmax_flat, [hparams.batch_size, -1, num_classes])

      tf.add_to_collection('inputs', inputs)
      tf.add_to_collection('temperature', temperature)
      tf.add_to_collection('softmax', softmax)

      for layer in range(num_layers):
        tf.add_to_collection('targets', targets[layer])
        tf.add_to_collection('past_targets', past_targets[layer])

        # Flatten state tuples for metagraph compatibility.
        for state in tf_nest.flatten(initial_state[layer]):
          tf.add_to_collection('initial_state', state)
        for state in tf_nest.flatten(final_state[layer]):
          tf.add_to_collection('final_state', state)

  return graph
