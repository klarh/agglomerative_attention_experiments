"""
This module uses lightly modified code from the keras-transformer
project, made available under the MIT license reproduced below.

The MIT License

Copyright 2018 Kirill Mavreshko (https://www.linkedin.com/in/kirill-mavreshko/)

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is furnished
to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included
in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE
OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

import flowws
from flowws import Argument as Arg

import keras
import keras.backend as K
from keras.layers import Input, Softmax, Embedding, Add, Lambda, Dense
from keras_transformer.extras import ReusableEmbedding, TiedOutputEmbedding
from keras_transformer.position import TransformerCoordinateEmbedding
from keras_transformer.transformer import TransformerACT, TransformerBlock

def maybe_setup_tensorflow():
    if keras.backend.backend() != 'tensorflow':
        return

    import tensorflow as tf

    tf_config = tf.ConfigProto()
    tf_config.gpu_options.allow_growth = True
    tf_config.graph_options.optimizer_options.global_jit_level = tf.OptimizerOptions.ON_1
    session = tf.Session(config=tf_config)
    K.set_session(session)

def universal_transformer_gpt_model(
        max_seq_length: int, vocabulary_size: int,
        word_embedding_size: int, transformer_depth: int,
        num_heads: int, transformer_dropout: float = 0.1,
        embedding_dropout: float = 0.6,
        l2_reg_penalty: float = 1e-6,
        confidence_penalty_weight: float = 0.1,
        agglomerative_attention: bool = False,
        use_convolutions: bool = False,
        use_coordinate_embeddings: bool = True,
        convolution_width: int = 0,
        penalize_confidence: bool = False):
    """
    A model which is similar to the one described by OpenAI in paper
    "Improving Language Understanding by Generative Pre-Training", except
    that it relies L2 regularization of the word embedding matrix
    (instead of the dropout), and uses Universal Transformer architecture.
    Derived from the keras-transformer project examples.
    """
    word_ids = Input(shape=(max_seq_length,), dtype='int32', name='word_ids')
    l2_regularizer = (keras.regularizers.l2(l2_reg_penalty) if l2_reg_penalty
                      else None)
    embedding_layer = ReusableEmbedding(
        vocabulary_size, word_embedding_size,
        input_length=max_seq_length,
        name='bpe_embeddings',
        # Regularization is based on paper "A Comparative Study on
        # Regularization Strategies for Embedding-based Neural Networks"
        # https://arxiv.org/pdf/1508.03721.pdf
        embeddings_regularizer=l2_regularizer)
    output_layer = TiedOutputEmbedding(
        projection_regularizer=l2_regularizer,
        projection_dropout=embedding_dropout,
        name='word_prediction_logits')
    conv_layer = keras.layers.Conv1D(
        word_embedding_size, convolution_width, padding='causal',
        activation='relu', kernel_initializer='he_uniform', name='convolution')
    coordinate_embedding_layer = TransformerCoordinateEmbedding(
        transformer_depth,
        name='coordinate_embedding')
    transformer_block = TransformerBlock(
        name='transformer', num_heads=num_heads,
        residual_dropout=transformer_dropout,
        attention_dropout=transformer_dropout,
        use_masking=True, vanilla_wiring=False,
        agglomerative_attention=agglomerative_attention)
    transformer_act_layer = TransformerACT(name='adaptive_computation_time')
    output_softmax_layer = Softmax(name='word_predictions')

    next_step_input, embedding_matrix = embedding_layer(word_ids)
    act_output = next_step_input

    for i in range(transformer_depth):
        if use_convolutions:
            next_step_input = conv_layer(next_step_input)
        if use_coordinate_embeddings:
            next_step_input = coordinate_embedding_layer(next_step_input, step=i)
        next_step_input = transformer_block(next_step_input)
        next_step_input, act_output = transformer_act_layer(next_step_input)

    transformer_act_layer.finalize()
    next_step_input = act_output
    word_predictions = output_softmax_layer(
        output_layer([next_step_input, embedding_matrix]))
    model = keras.models.Model(inputs=[word_ids], outputs=[word_predictions])
    # Penalty for confidence of the output distribution, as described in
    # "Regularizing Neural Networks by Penalizing Confident
    # Output Distributions" (https://arxiv.org/abs/1701.06548)
    confidence_penalty = K.mean(
        confidence_penalty_weight *
        K.sum(word_predictions * K.log(word_predictions), axis=-1))
    if penalize_confidence:
        model.add_loss(confidence_penalty)
    return model

@flowws.add_stage_arguments
class GPTModel(flowws.Stage):
    """Train a model on the wikitext-2 dataset"""

    ARGS = [
        Arg('width', '-w', int, 64,
            help='Working width of the deep network'),
        Arg('depth', '-d', int, 6,
            help='Number of transformer blocks to use'),
        Arg('use_convolutions', '-c', bool, False,
            help='Use causal convolutions instead of position embeddings'),
        Arg('use_agglomeration', '-a', bool, False,
            help='Use agglomerative instead of full attention'),
        Arg('convolution_width', None, int, 8,
            help='Width of causal convolutions to use'),
        Arg('num_heads', '-n', int, 8,
            help='Number of attention/agglomerative heads to use'),
        Arg('print_summary', '-p', bool, False,
            help='Print a summary of the model before continuing'),
    ]

    def run(self, scope, storage):
        vocabulary_size = scope['vocabulary_size']
        sequence_length = scope['sequence_length']

        maybe_setup_tensorflow()

        model = universal_transformer_gpt_model(
            sequence_length,
            vocabulary_size,
            self.arguments['width'],
            self.arguments['depth'],
            self.arguments['num_heads'],
            agglomerative_attention=self.arguments['use_agglomeration'],
            use_convolutions=self.arguments['use_convolutions'],
            use_coordinate_embeddings=(not self.arguments['use_convolutions']),
            convolution_width=self.arguments['convolution_width'])

        if self.arguments['print_summary']:
            model.summary()

        scope['model'] = model
