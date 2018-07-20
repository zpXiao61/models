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
"""Defines NeuMF model for NCF framework.

Some abbreviations used in the code base:
NeuMF: Neural Matrix Factorization
NCF: Neural Collaborative Filtering
GMF: Generalized Matrix Factorization
MLP: Multi-Layer Perceptron

GMF applies a linear kernel to model the latent feature interactions, and MLP
uses a nonlinear kernel to learn the interaction function from data. NeuMF model
is a fused model of GMF and MLP to better model the complex user-item
interactions, and unifies the strengths of linearity of MF and non-linearity of
MLP for modeling the user-item latent structures.

In NeuMF model, it allows GMF and MLP to learn separate embeddings, and combine
the two models by concatenating their last hidden layer.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import typing

from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

from official.datasets import movielens  # pylint: disable=g-bad-import-order


def neumf_model_fn(features, labels, mode, params):
  """Model Function for NeuMF estimator."""
  users = tf.squeeze(features[movielens.USER_COLUMN])
  items = tf.squeeze(features[movielens.ITEM_COLUMN])
  labels = tf.cast(labels, tf.float32)

  num_users = params["num_users"]
  num_items = params["num_items"]

  model_layers = params["model_layers"]

  mf_regularization = params["mf_regularization"]
  mlp_reg_layers = params["mlp_reg_layers"]

  mf_dim = params["mf_dim"]

  model = NeuMF(num_users=num_users, num_items=num_items, mf_dim=mf_dim,
                model_layers=model_layers, mf_regularization=mf_regularization,
                mlp_reg_layers=mlp_reg_layers, final_activation=None)
  logits = model([users, items])

  if mode == tf.estimator.ModeKeys.PREDICT:
    predictions = {
        movielens.RATING_COLUMN: tf.sigmoid(logits[:features["n"]]),
    }

    # return tf.estimator.EstimatorSpec(
    #     mode=tf.estimator.ModeKeys.PREDICT,
    #     predictions=predictions
    # )
    return tf.contrib.tpu.TPUEstimatorSpec(
        mode=tf.estimator.ModeKeys.PREDICT,
        predictions=predictions
    )

  elif mode == tf.estimator.ModeKeys.TRAIN:
    optimizer = tf.train.AdamOptimizer(learning_rate=params["learning_rate"])
    optimizer = tf.contrib.tpu.CrossShardOptimizer(optimizer)

    losses = tf.nn.sigmoid_cross_entropy_with_logits(
        labels=tf.cast(tf.squeeze(labels), tf.float32),
        logits=tf.cast(tf.squeeze(logits), tf.float32)
    )
    loss = tf.reduce_mean(losses)

    global_step = tf.train.get_global_step()
    tvars = tf.trainable_variables()
    gradients = optimizer.compute_gradients(
        loss, tvars, colocate_gradients_with_ops=True)
    minimize_op = optimizer.apply_gradients(
        gradients, global_step=global_step, name="train")
    update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
    train_op = tf.group(minimize_op, update_ops)

    # return tf.estimator.EstimatorSpec(mode=mode, loss=loss, train_op=train_op)
    return tf.contrib.tpu.TPUEstimatorSpec(
        mode=mode, loss=loss, train_op=train_op,
    )

  else:
    raise NotImplementedError


class NeuMF(tf.keras.models.Model):
  """Neural matrix factorization (NeuMF) model for recommendations."""

  def __init__(self, num_users, num_items, mf_dim, model_layers,
               mf_regularization, mlp_reg_layers, final_activation="sigmoid"):
    # type: (int, int, int, list, float, list, typing.Union[str, None]) -> None
    """Initialize NeuMF model.

    Args:
      num_users: An integer, the number of users.
      num_items: An integer, the number of items.
      mf_dim: An integer, the embedding size of Matrix Factorization (MF) model.
      model_layers: A list of integers for Multi-Layer Perceptron (MLP) layers.
        Note that the first layer is the concatenation of user and item
        embeddings. So model_layers[0]//2 is the embedding size for MLP.
      mf_regularization: A floating number, the regularization factor for MF
        embeddings.
      mlp_reg_layers: A list of floating numbers, the regularization factors for
        each layer in MLP.

    Raises:
      ValueError: if the first model layer is not even.
    """
    if model_layers[0] % 2 != 0:
      raise ValueError("The first layer size should be multiple of 2!")

    # Input variables
    user_input = tf.keras.layers.Input(
        shape=(1,), dtype=tf.int64, name=movielens.USER_COLUMN)
    item_input = tf.keras.layers.Input(
        shape=(1,), dtype=tf.int64, name=movielens.ITEM_COLUMN)

    # Initializer for embedding layers
    embedding_initializer = "glorot_uniform"

    # Embedding layers of GMF and MLP
    mf_embedding_user = tf.keras.layers.Embedding(
        num_users,
        mf_dim,
        embeddings_initializer=embedding_initializer,
        embeddings_regularizer=tf.keras.regularizers.l2(mf_regularization),
        input_length=1)
    mf_embedding_item = tf.keras.layers.Embedding(
        num_items,
        mf_dim,
        embeddings_initializer=embedding_initializer,
        embeddings_regularizer=tf.keras.regularizers.l2(mf_regularization),
        input_length=1)

    mlp_embedding_user = tf.keras.layers.Embedding(
        num_users,
        model_layers[0]//2,
        embeddings_initializer=embedding_initializer,
        embeddings_regularizer=tf.keras.regularizers.l2(mlp_reg_layers[0]),
        input_length=1)
    mlp_embedding_item = tf.keras.layers.Embedding(
        num_items,
        model_layers[0]//2,
        embeddings_initializer=embedding_initializer,
        embeddings_regularizer=tf.keras.regularizers.l2(mlp_reg_layers[0]),
        input_length=1)

    # GMF part
    # Flatten the embedding vector as latent features in GMF
    mf_user_latent = tf.keras.layers.Flatten()(mf_embedding_user(user_input))
    mf_item_latent = tf.keras.layers.Flatten()(mf_embedding_item(item_input))
    # Element-wise multiply
    mf_vector = tf.keras.layers.multiply([mf_user_latent, mf_item_latent])

    # MLP part
    # Flatten the embedding vector as latent features in MLP
    mlp_user_latent = tf.keras.layers.Flatten()(mlp_embedding_user(user_input))
    mlp_item_latent = tf.keras.layers.Flatten()(mlp_embedding_item(item_input))
    # Concatenation of two latent features
    mlp_vector = tf.keras.layers.concatenate([mlp_user_latent, mlp_item_latent])

    num_layer = len(model_layers)  # Number of layers in the MLP
    for layer in xrange(1, num_layer):
      model_layer = tf.keras.layers.Dense(
          model_layers[layer],
          kernel_regularizer=tf.keras.regularizers.l2(mlp_reg_layers[layer]),
          activation="relu")
      mlp_vector = model_layer(mlp_vector)

    # Concatenate GMF and MLP parts
    predict_vector = tf.keras.layers.concatenate([mf_vector, mlp_vector])

    # Final prediction layer
    prediction = tf.keras.layers.Dense(
        1, activation=final_activation, kernel_initializer="lecun_uniform",
        name=movielens.RATING_COLUMN)(predict_vector)

    super(NeuMF, self).__init__(
        inputs=[user_input, item_input], outputs=prediction)
