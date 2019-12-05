import numpy as np
import tensorflow as tf
from tensorflow.keras.layers import Concatenate, Dense, Layer, LSTM
from typing import Tuple


class DNC(tf.keras.Model):

    def __init__(self,
                 output_dim: int,
                 memory_shape: tuple = (100, 20),
                 n_read: int = 3,
                 name: str = 'dnc'
                 ) -> None:
        """
        Initialize DNC object.

        Parameters
        ----------
        output_dim
            Size of output vector.
        memory_shape
            Shape of memory matrix (rows, cols).
        n_read
            Number of read heads.
        name
            Name of DNC.
        """
        super(DNC, self).__init__(name=name)

        # define output data size
        self.output_dim = output_dim  # Y

        # define size of memory matrix
        self.N, self.W = memory_shape  # N, W

        # define number of read heads
        self.R = n_read  # R

        # size of output vector from controller that defines interactions with memory matrix:
        # R read keys + R read strengths + write key + write strength + erase vector +
        # write vector + R free gates + allocation gate + write gate + R read modes
        self.interface_dim = self.R * self.W + 3 * self.W + 5 * self.R + 3  # I

        # neural net output = output of controller + interface vector with memory
        self.controller_dim = self.output_dim + self.interface_dim  # Y+I

        # initialize controller output and interface vector with gaussian normal
        self.output = tf.truncated_normal([1, self.output_dim], stddev=0.1)  # [1,Y]
        self.interface = tf.truncated_normal([1, self.interface_dim], stddev=0.1)  # [1,I]

        # initialize memory matrix with zeros
        self.M = tf.zeros(memory_shape)  # [N,W]

        # usage vector records which locations in the memory are used and which are free
        self.usage = tf.fill([self.N, 1], 1e-6)  # [N,1]

        # temporal link matrix L[i,j] records to which degree location i was written to after j
        self.L = tf.zeros([self.N, self.N])  # [N,N]

        # precedence vector determines degree to which a memory row was written to at t-1
        self.W_precedence = tf.zeros([self.N, 1])  # [N,1]

        # initialize R read weights and vectors and write weights
        self.W_read = tf.fill([self.N, self.R], 1e-6)  # [N,R]
        self.W_write = tf.fill([self.N, 1], 1e-6)  # [N,1]
        self.read_v = tf.fill([self.R, self.W], 1e-6)  # [R,W]  # TODO: chg name

        # controller variables
        # initialize controller hidden state
        self.h = tf.Variable(tf.truncated_normal([1, self.controller_dim], stddev=0.1), name='dnc_h')  # [1,Y+I]
        self.c = tf.Variable(tf.truncated_normal([1, self.controller_dim], stddev=0.1), name='dnc_c')  # [1,Y+I]

        # define and initialize weights for controller output and interface vectors
        self.W_output = tf.Variable(  # [Y+I,Y]
            tf.truncated_normal([self.controller_dim, self.output_dim], stddev=0.1),
            name='dnc_net_output_weights'
        )
        self.W_interface = tf.Variable(  # [Y+I,I]
            tf.truncated_normal([self.controller_dim, self.interface_dim], stddev=0.1),
            name='dnc_interface_weights'
        )

        # output y = v + W_read_out[r(1), ..., r(R)]
        self.W_read_out = tf.Variable(  # [R*W,Y]
            tf.truncated_normal([self.R * self.W, self.output_dim], stddev=0.1),
            name='dnc_read_vector_weights'
        )

    def content_lookup(self, key: tf.Tensor, strength: tf.Tensor) -> tf.Tensor:
        """
        Attention mechanism: content based addressing to read from and write to the memory.

        Params
        ------
        key
            Key vector emitted by the controller and used to calculate row-by-row
            cosine similarity with the memory matrix.
        strength
            Strength scalar attached to each key vector (1x1 or 1xR).

        Returns
        -------
        Similarity measure for each row in the memory used by the read heads for associative
        recall or by the write head to modify a vector in memory.
        """
        # The l2 norm applied to each key and each row in the memory matrix
        norm_mem = tf.nn.l2_normalize(self.M, 1)  # N*W
        norm_key = tf.nn.l2_normalize(key, 1)  # 1*W for write or R*W for read

        # get similarity measure between both vectors, transpose before multiplication
        # (N*W,W*1)->N*1 for write
        # (N*W,W*R)->N*R for read
        sim = tf.matmul(norm_mem, norm_key, transpose_b=True)
        return tf.nn.softmax(sim * strength, 0)  # N*1 or N*R

    def allocation_weighting(self) -> tf.Tensor:
        """
        Memory needs to be freed up and allocated in a differentiable way.
        The usage vector shows how much each memory row is used.
        Unused rows can be written to. Usage of a row increases if
        we write to it and can decrease if we read from it, depending on the free gates.
        Allocation weights are then derived from the usage vector.

        Returns
        -------
        Allocation weights for each row in the memory.
        """
        # sort usage vector in ascending order and keep original indices of sorted usage vector
        sorted_usage_vec, free_list = tf.nn.top_k(-1 * tf.transpose(self.usage), k=self.N)
        sorted_usage_vec *= -1
        cumprod = tf.cumprod(sorted_usage_vec, axis=1, exclusive=True)
        unorder = (1 - sorted_usage_vec) * cumprod

        alloc_weights = tf.zeros([self.N])
        I = tf.constant(np.identity(self.N, dtype=np.float32))

        # for each usage vec
        for pos, idx in enumerate(tf.unstack(free_list[0])):
            # flatten
            m = tf.squeeze(tf.slice(I, [idx, 0], [1, -1]))
            # add to allocation weight matrix
            alloc_weights += m * unorder[0, pos]
        # return the allocation weighting for each row in memory
        return tf.reshape(alloc_weights, [self.N, 1])

    def controller(self, x: tf.Tensor) -> None:
        # flatten input and pass through dense layer to avoid shape mismatch
        x = tf.reshape(x, [1, -1])
        x = Dense(self.W, activation=None)(x)  # [1,W]

        # concatenate input with read vectors
        x_in = tf.expand_dims(Concatenate()([x, self.read_v], axis=0), axis=0)  # [1,R+1,W]

        # LSTM controller
        initial_state = [self.h, self.c]
        _ , self.h, self.c = LSTM(self.controller_dim,
                                  return_sequences=False,
                                  return_state=True,
                                  name='dnc_controller')(x_in, initial_state=initial_state)

    def partition_interface(self):
        # convert interface vector into a set of read write vectors
        partition = tf.constant([[0] * (self.R * self.W) + [1] * self.R +
                                 [2] * self.W + [3] + [4] * self.W + [5] * self.W +
                                 [6] * self.R + [7] + [8] + [9] * (self.R * 3)],
                                dtype=tf.int32)

        (k_read, b_read, k_write, b_write, erase, write_v, free_gates, alloc_gate,
         write_gate, read_modes) = tf.dynamic_partition(self.interface, partition, 10)

        # R read keys and strengths
        k_read = tf.reshape(k_read, [self.R, self.W])  # [R,W]
        b_read = 1 + tf.nn.softplus(tf.expand_dims(b_read, 0))  # [1,R]

        # write key, strength, erase and write vectors
        k_write = tf.expand_dims(k_write, 0)  # [1,W]
        b_write = 1 + tf.nn.softplus(tf.expand_dims(b_write, 0))  # [1,1]
        erase = tf.nn.sigmoid(tf.expand_dims(erase, 0))  # [1,W]
        write_v = tf.expand_dims(write_v, 0)  # [1,W]

        # the degree to which locations at read heads will be freed
        free_gates = tf.nn.sigmoid(tf.expand_dims(free_gates, 0))  # [1,R]

        # the fraction of writing that is being allocated in a new location
        alloc_gate = tf.reshape(tf.nn.sigmoid(alloc_gate), [1])  # 1

        # the amount of information to be written to memory
        write_gate = tf.reshape(tf.nn.sigmoid(write_gate), [1])  # 1

        # softmax distribution over the 3 read modes (forward, content lookup, backward)
        read_modes = tf.reshape(read_modes, [3, self.R])  # [3,R]
        read_modes = tf.nn.softmax(read_modes, axis=0)

        return (k_read, b_read, k_write, b_write, erase, write_v,
                free_gates, alloc_gate, write_gate, read_modes)

    def write(self):
        pass

    def read(self):
        pass

    def call(self, x: tf.Tensor) -> tf.Tensor:
        # update controller
        self.controller(x)

        # create output and interface vectors
        self.output = tf.matmul(self.h, self.W_output)  # [1,Y+I] * [Y+I,Y] -> [1,Y]
        self.interface = tf.matmul(self.h, self.W_interface)  # [1,Y+I] * [Y+I,I] -> [1,I]

        # partition the interface vector
        (k_read, b_read, k_write, b_write, erase, write_v,
         free_gates, alloc_gate, write_gate, read_modes) = self.partition_interface()

        # write to memory
        self.write()

        # read from memory
        self.read()
        # compute output
        pass
