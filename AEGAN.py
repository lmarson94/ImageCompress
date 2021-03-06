import tensorflow as tf
import numpy as np
import matplotlib.pyplot as plt

# reset graph
tf.reset_default_graph()

# Input functions ------------------------------------------------------------------------------------------------------


def _parse_function(example_proto):
    keys_to_features = {'image/encoded': tf.VarLenFeature(tf.string),
                        'image/label': tf.VarLenFeature(tf.string)}
    parsed_features = tf.parse_example(example_proto, keys_to_features)
    raw1 = tf.sparse_tensor_to_dense(parsed_features['image/encoded'], default_value="0", )
    raw2 = tf.sparse_tensor_to_dense(parsed_features['image/label'], default_value="0", )

    return (tf.map_fn(decode, tf.squeeze(raw1), dtype=tf.uint8, back_prop=False),
            tf.map_fn(decode, tf.squeeze(raw2), dtype=tf.uint8, back_prop=False))


def decode(raw):
    return tf.image.decode_jpeg(raw, channels=3)


def get_train_dataset(path, batch_size):
    files = tf.data.Dataset.list_files(path)
    dataset = files.apply(tf.contrib.data.parallel_interleave(tf.data.TFRecordDataset, cycle_length=4))
    dataset = dataset.apply(tf.contrib.data.batch_and_drop_remainder(batch_size))
    dataset = dataset.apply(tf.contrib.data.shuffle_and_repeat(20))
    dataset = dataset.map(_parse_function, num_parallel_calls=4)
    dataset = dataset.prefetch(batch_size)

    return dataset

# MS-SSIM functions ------------------------------------------------------------------------------------------------------------------

def _tf_fspecial_gauss(size, sigma):
    """Function to mimic the 'fspecial' gaussian MATLAB function
    """
    x_data, y_data = np.mgrid[-size // 2 + 1:size // 2 + 1, -size // 2 + 1:size // 2 + 1]

    x_data = np.expand_dims(x_data, axis=-1)
    x_data = np.expand_dims(x_data, axis=-1)

    y_data = np.expand_dims(y_data, axis=-1)
    y_data = np.expand_dims(y_data, axis=-1)

    x = tf.constant(x_data, dtype=tf.float32)
    y = tf.constant(y_data, dtype=tf.float32)

    g = tf.exp(-((x ** 2 + y ** 2) / (2.0 * sigma ** 2)))
    return g / tf.reduce_sum(g)


def tf_ssim(img1, img2, cs_map=False, mean_metric=True, size=9, sigma=1.5):

    window = _tf_fspecial_gauss(size, sigma)  # window shape [size, size]
    K1 = 0.01
    K2 = 0.03
    L = 1  # depth of image (255 in case the image has a different scale)
    C1 = (K1 * L) ** 2
    C2 = (K2 * L) ** 2
    mu1 = tf.nn.conv2d(img1, window, strides=[1, 1, 1, 1], padding='VALID')
    mu2 = tf.nn.conv2d(img2, window, strides=[1, 1, 1, 1], padding='VALID')
    mu1_sq = mu1 * mu1
    mu2_sq = mu2 * mu2
    mu1_mu2 = mu1 * mu2
    sigma1_sq = tf.nn.conv2d(img1 * img1, window, strides=[1, 1, 1, 1], padding='VALID') - mu1_sq
    sigma2_sq = tf.nn.conv2d(img2 * img2, window, strides=[1, 1, 1, 1], padding='VALID') - mu2_sq
    sigma12 = tf.nn.conv2d(img1 * img2, window, strides=[1, 1, 1, 1], padding='VALID') - mu1_mu2
    if cs_map:
        value = (((2 * mu1_mu2 + C1) * (2 * sigma12 + C2)) / ((mu1_sq + mu2_sq + C1) * (sigma1_sq + sigma2_sq + C2)), (2.0 * sigma12 + C2) / (sigma1_sq + sigma2_sq + C2))
    else:
        value = ((2 * mu1_mu2 + C1) * (2 * sigma12 + C2)) / ((mu1_sq + mu2_sq + C1) * (sigma1_sq + sigma2_sq + C2))

    if mean_metric:
        value = tf.reduce_mean(value)

    return value


def tf_ms_ssim(img1, img2, mean_metric=True, level=5):

    weight = tf.constant([0.0448, 0.2856, 0.3001, 0.2363, 0.1333], dtype=tf.float32)
    mssim = []
    mcs = []
    for l in range(level):
        ssim_map, cs_map = tf_ssim(img1, img2, cs_map=True, mean_metric=False)
        mssim.append(tf.reduce_mean(ssim_map))
        mcs.append(tf.reduce_mean(cs_map))
        filtered_im1 = tf.nn.avg_pool(img1, [1, 2, 2, 1], [1, 2, 2, 1], padding='SAME')
        filtered_im2 = tf.nn.avg_pool(img2, [1, 2, 2, 1], [1, 2, 2, 1], padding='SAME')
        img1 = filtered_im1
        img2 = filtered_im2

    # list to tensor of dim D+1
    mssim = tf.stack(mssim, axis=0)
    mcs = tf.stack(mcs, axis=0)

    value = (tf.reduce_prod(mcs[0:level - 1] ** weight[0:level - 1]) * (mssim[level - 1] ** weight[level - 1]))

    if mean_metric:
        value = tf.reduce_mean(value)

    return value


# Context Model functions ------------------------------------------------------------------------------------------------------------------

def get_weights(name, shape, mask_filter):

    weights_initializer1 = tf.contrib.layers.xavier_initializer()

    W = tf.get_variable(name, shape, tf.float32, weights_initializer1)

    W = W * mask_filter

    return W


def get_bias(name, shape):

    return tf.get_variable(name, shape, tf.float32, tf.constant_initializer(0.1, dtype=tf.float32))


def conv3d(z, W):

    return tf.nn.conv3d(z, W, strides=[1, 1, 1, 1, 1], padding='SAME')


def H_context_model(P, best_centroids, L, t_primo, beta, z):

    shape2 = z.get_shape().as_list()

    X2, Y2, Z2, W2 = tf.meshgrid(np.arange(shape2[0]), np.arange(shape2[1]), np.arange(shape2[2]), np.arange(shape2[3]))
    idx2 = tf.transpose(tf.stack([tf.reshape(X2, [-1]), tf.reshape(Y2, [-1]), tf.reshape(Z2, [-1]), tf.reshape(W2, [-1]), tf.reshape(best_centroids, [-1])]))

    log_base_change_factor_cm = tf.constant(np.log2(np.e), dtype=tf.float32)

    h = tf.reduce_sum(- log_base_change_factor_cm * tf.log(tf.maximum(1e-9, tf.reshape(tf.gather_nd(P, idx2), [shape2[0], shape2[1], shape2[2], shape2[3]]))), axis=[3, 2, 1])

    return tf.reduce_sum(tf.maximum(0.0, beta * (h - t_primo)))


# Mask and Quantization functions ------------------------------------------------------------------------------------------------------------------

def Mask(y, K):

    yy = tf.transpose(tf.reshape(tf.tile(tf.reshape(y, [-1]), [K]), [K, tf.shape(y)[0], tf.shape(y)[1], tf.shape(y)[2]]), [1, 2, 3, 0])
    kk = tf.transpose(tf.reshape(tf.tile(tf.linspace(0., K-1, K), [np.prod(y.get_shape().as_list())]), [tf.shape(y)[0], tf.shape(y)[1], tf.shape(y)[2], K]), [0, 1, 2, 3])
    m = yy - kk
    z = tf.zeros(tf.shape(m))
    m = tf.maximum(x=m, y=z)
    z = tf.ones(tf.shape(m))
    m = tf.minimum(x=m, y=z)

    # gradient trick
    m = m + tf.stop_gradient(tf.ceil(m) - m)

    return m


def Q(z_masked, centroids, L, z):

    zz = tf.transpose(tf.reshape(tf.tile(
        tf.reshape(z_masked, [-1]), [L]), [L, tf.shape(z_masked)[0], tf.shape(z_masked)[1], tf.shape(z_masked)[2], tf.shape(z_masked)[3]]), [1, 2, 3, 4, 0])
    cc = tf.reshape(tf.tile(centroids, [tf.size(z_masked)]), [tf.shape(z_masked)[0], tf.shape(z_masked)[1], tf.shape(z_masked)[2], tf.shape(z_masked)[3], L])
    best_centroids = tf.argmin(tf.abs(zz - cc), axis=-1)

    shape = z.get_shape().as_list()

    X, Y, Z, W = tf.meshgrid(np.arange(shape[0]), np.arange(shape[1]), np.arange(shape[2]), np.arange(shape[3]))
    idx = tf.transpose(tf.stack([tf.reshape(X, [-1]), tf.reshape(Y, [-1]), tf.reshape(Z, [-1]), tf.reshape(W, [-1]), tf.reshape(best_centroids, [-1])]))
    z_hat = tf.reshape(tf.gather_nd(cc, idx), [shape[0], shape[1], shape[2], shape[3]])

    return (z_hat, best_centroids)


def H(m, P, best_centroids, L, t_primo, beta, z):

    shape = z.get_shape().as_list()

    X, Y, Z, W = tf.meshgrid(np.arange(shape[0]), np.arange(shape[1]), np.arange(shape[2]), np.arange(shape[3]))
    idx = tf.transpose(tf.stack([tf.reshape(X, [-1]), tf.reshape(Y, [-1]), tf.reshape(Z, [-1]), tf.reshape(W, [-1]), tf.reshape(best_centroids, [-1])]))

    log_base_change_factor = tf.constant(np.log2(np.e), dtype=tf.float32)

    h = tf.reduce_sum(- m * log_base_change_factor * tf.log(tf.maximum(1e-9, tf.reshape(tf.gather_nd(P, idx), [shape[0], shape[1], shape[2], shape[3]]))), axis=[3, 2, 1])

    return tf.reduce_sum(tf.maximum(0.0, beta * (h - t_primo)))

def getMSSSIM(x, x_hat):

    msssim_indexR = tf_ms_ssim(x[:, :, :, 0:1], x_hat[:, :, :, 0:1])
    msssim_indexG = tf_ms_ssim(x[:, :, :, 1:2], x_hat[:, :, :, 1:2])
    msssim_indexB = tf_ms_ssim(x[:, :, :, 2:3], x_hat[:, :, :, 2:3])
    acc = (msssim_indexR + msssim_indexG + msssim_indexB) / 3

    return acc

def getAlpha(acc):

    return 2000*((acc - 1)**2) + 5


def discriminator(x, Dregularizer, DregularizerDense, batch_size, reuse_variables=None):

    with tf.variable_scope(tf.get_variable_scope(), reuse=reuse_variables) as scope:
        Dconv1 = tf.layers.conv2d(inputs=x,
                                  filters=64,
                                  kernel_size=[3, 3],
                                  strides=(2, 2),
                                  padding="same",
                                  kernel_regularizer=Dregularizer,
                                  name="Dconv1")

        Dconv1 = tf.layers.batch_normalization(Dconv1)
        Dconv1 = tf.nn.relu(Dconv1)

        Dconv2 = tf.layers.conv2d(inputs=Dconv1,
                                  filters=128,
                                  kernel_size=[3, 3],
                                  strides=(2, 2),
                                  padding="same",
                                  kernel_regularizer=Dregularizer,
                                  name="Dconv2")

        Dconv2 = tf.layers.batch_normalization(Dconv2)
        Dconv2 = tf.nn.relu(Dconv2)

        Dconv3 = tf.layers.conv2d(inputs=Dconv2,
                                  filters=256,
                                  kernel_size=[3, 3],
                                  strides=(2, 2),
                                  padding="same",
                                  kernel_regularizer=Dregularizer,
                                  name="Dconv3")

        Dconv3 = tf.layers.batch_normalization(Dconv3)
        Dconv3 = tf.nn.relu(Dconv3)

        Dconv4 = tf.layers.conv2d(inputs=Dconv3,
                                  filters=128,
                                  kernel_size=[3, 3],
                                  strides=(2, 2),
                                  padding="same",
                                  kernel_regularizer=Dregularizer,
                                  name="Dconv4")

        Dconv4 = tf.layers.batch_normalization(Dconv4)
        Dconv4 = tf.nn.relu(Dconv4)

        Dconv5 = tf.layers.conv2d(inputs=Dconv4,
                                  filters=32,
                                  kernel_size=[3, 3],
                                  strides=(2, 2),
                                  padding="same",
                                  kernel_regularizer=Dregularizer,
                                  name="Dconv5")

        Dconv5 = tf.layers.batch_normalization(Dconv5)
        Dconv5 = tf.nn.relu(Dconv5)

        DconvOut = tf.reshape(Dconv5, [batch_size, -1])

        Ddense1 = tf.layers.dense(inputs=DconvOut,
                                  units=1024,
                                  activation=tf.nn.relu,
                                  # kernel_initializer=tf.contrib.layers.xavier_initializer,
                                  kernel_regularizer=DregularizerDense,
                                  name="Ddense1")

        Ddense2 = tf.layers.dense(inputs=Ddense1,
                                  units=512,
                                  activation=tf.nn.relu,
                                  # kernel_initializer=tf.contrib.layers.xavier_initializer,
                                  kernel_regularizer=DregularizerDense,
                                  name="Ddense2")

        Dout = tf.layers.dense(inputs=Ddense2,
                               units=1,
                               activation=None,
                               # kernel_initializer=tf.contrib.layers.xavier_initializer,
                               kernel_regularizer=DregularizerDense,
                               name="Dout")

        return Dout

# variables initialization ------------------------------------------------------------------------------------------------------------------

# network hyper-parameter

t_primo = tf.constant(0.4)  # Clipping term for entropy
sigma = tf.constant(1.)
depth = 5  # Depth residual block for the AutoEncoder
regularizer = tf.contrib.layers.l2_regularizer(scale=0.01)  # Regularization term for all layers
regularizer2 = tf.contrib.layers.l2_regularizer(scale=0.1)  # Regularization term for layer that outputs y
image_height = 160
image_width = 160


K = 32
n_centroids = 6
beta = 500
L = n_centroids


# constanti
Dregularizer = tf.contrib.layers.l2_regularizer(scale=0.001)
DregularizerDense = tf.contrib.layers.l2_regularizer(scale=0.001)
Gregularizer = tf.contrib.layers.l2_regularizer(scale=0.001)
Ginitializer = tf.random_normal_initializer(stddev=0.00001)
lrD = tf.Variable(5e-4, dtype=tf.float32)
lrG = tf.Variable(5e-4, dtype=tf.float32)
epochsD = 1
epochsGAN = 10
batch_size = 30



mask_filter1 = tf.constant([[[[[1, 1, 1], [1, 1, 1], [1, 1, 1]], [[1, 1, 1], [1, 0, 0], [0, 0, 0]], [[0, 0, 0], [0, 0, 0], [0, 0, 0]]]]], dtype=tf.float32)
mask_filter2 = tf.constant([[[[[1, 1, 1], [1, 1, 1], [1, 1, 1]], [[1, 1, 1], [1, 1, 0], [0, 0, 0]], [[0, 0, 0], [0, 0, 0], [0, 0, 0]]]]], dtype=tf.float32)
mask_filter3 = tf.constant([[[[[1, 1, 1], [1, 1, 1], [1, 1, 1]], [[1, 1, 1], [1, 1, 0], [0, 0, 0]], [[0, 0, 0], [0, 0, 0], [0, 0, 0]]]]], dtype=tf.float32)
mask_filter1 = tf.transpose(mask_filter1, [2, 3, 4, 0, 1])
mask_filter2 = tf.transpose(mask_filter2, [2, 3, 4, 0, 1])
mask_filter3 = tf.transpose(mask_filter3, [2, 3, 4, 0, 1])
mask_filter1 = tf.reshape(tf.tile(mask_filter1, [1, 1, 1, 1, 24]), [3, 3, 3, 1, 24])
mask_filter2 = tf.reshape(tf.tile(mask_filter2, [1, 1, 1, 24, 24]), [3, 3, 3, 24, 24])
mask_filter3 = tf.reshape(tf.tile(mask_filter3, [1, 1, 1, 24, L]), [3, 3, 3, 24, L])


# dataset and iterator initialization

dataset = get_train_dataset("/mnt/disks/disk2/ae_out/records/train/train-*", batch_size)

iterator = tf.data.Iterator.from_structure(dataset.output_types,
                                           dataset.output_shapes)

with tf.device('/cpu:0'):
    data = iterator.get_next()
    x_ae = tf.reshape(tf.image.convert_image_dtype(data[0], dtype=tf.float32), [batch_size, 160, 160, 3])
    x = tf.reshape(tf.image.convert_image_dtype(data[1], dtype=tf.float32), [batch_size, 160, 160, 3])


# Graph definition ------------------------------------------------------------------------------------------------------------------

# Network Placeholders
training = tf.placeholder(dtype=tf.bool, shape=(), name="isTraining")
file_path = tf.placeholder(tf.string, name="path")


# Encoder

centroids = tf.get_variable(name="centroid", shape=(L,), dtype=tf.float32, initializer=tf.random_uniform_initializer(minval=-2, maxval=2, seed=666))

[mean, var] = tf.nn.moments(x, axes=[0, 1, 2])
mean = tf.transpose(tf.expand_dims(tf.expand_dims(tf.expand_dims(mean, -1), -1), -1), [1, 2, 3, 0])
var = tf.transpose(tf.expand_dims(tf.expand_dims(tf.expand_dims(var, -1), -1), -1), [1, 2, 3, 0])

x_n = (x - mean) / tf.sqrt(var + 1e-10)

conv1 = tf.layers.conv2d(inputs=x_n,
                         filters=64,
                         kernel_size=[5, 5],
                         strides=(2, 2),
                         padding="same",
                         kernel_regularizer=regularizer,
                         name="conv1")

conv1 = tf.layers.batch_normalization(inputs=conv1, training=training)
conv1 = tf.nn.relu(conv1)
conv2 = tf.layers.conv2d(inputs=conv1,
                         filters=128,
                         kernel_size=[5, 5],
                         strides=(2, 2),
                         padding="same",
                         kernel_regularizer=regularizer,
                         name="conv2")

conv2 = tf.layers.batch_normalization(inputs=conv2, training=training)
conv2 = tf.nn.relu(conv2)

E_residual_blocks = []
tmp = conv2
for i in range(depth):
    tmp3 = tmp
    for j in range(3):
        tmp2 = tmp
        E_residual_blocks.append(tf.layers.conv2d(inputs=tmp,
                                                  filters=128,
                                                  kernel_size=[3, 3],
                                                  strides=(1, 1),
                                                  padding="same",
                                                  kernel_regularizer=regularizer,
                                                  name="conv"+str(6*i+2*j+3)))

        E_residual_blocks[-1] = tf.layers.batch_normalization(inputs=E_residual_blocks[-1], training=training)
        E_residual_blocks[-1] = tf.nn.relu(E_residual_blocks[-1])
        tmp = E_residual_blocks[-1]
        E_residual_blocks.append(tf.layers.conv2d(inputs=tmp,
                                                  filters=128,
                                                  kernel_size=[3, 3],
                                                  strides=(1, 1),
                                                  padding="same",
                                                  kernel_regularizer=regularizer,
                                                  name="conv"+str(6*i+2*j+4)))

        tmp = E_residual_blocks[-1] + tmp2
    tmp = tmp3 + tmp

tmp2 = tmp
E_residual_blocks.append(tf.layers.conv2d(inputs=tmp,
                                          filters=128,
                                          kernel_size=[3, 3],
                                          strides=(1, 1),
                                          padding="same",
                                          kernel_regularizer=regularizer,
                                          name="conv"+str(depth*6+3)))

E_residual_blocks[-1] = tf.layers.batch_normalization(inputs=E_residual_blocks[-1], training=training)
E_residual_blocks[-1] = tf.nn.relu(E_residual_blocks[-1])
tmp = E_residual_blocks[-1]
E_residual_blocks.append(tf.layers.conv2d(inputs=tmp,
                                          filters=128,
                                          kernel_size=[3, 3],
                                          strides=(1, 1),
                                          padding="same",
                                          kernel_regularizer=regularizer,
                                          name="conv"+str(depth*6+4)))

tmp = E_residual_blocks[-1] + tmp2 + conv2

e_out = tf.layers.conv2d(inputs=tmp,
                         filters=K,
                         kernel_size=[5, 5],
                         strides=(2, 2),
                         padding="same",
                         kernel_regularizer=regularizer,
                         name="conv"+str(depth*6+5))

y_out = tf.layers.conv2d(inputs=tmp,
                         filters=1,
                         kernel_size=[5, 5],
                         strides=(2, 2),
                         padding="same",
                         #kernel_initializer=tf.constant_initializer(K),
                         kernel_regularizer=regularizer2,
                         name="conv"+str(depth * 6 + 6))
z = e_out
y = tf.sigmoid(tf.nn.relu(y_out)) * K

# Quantizer

m = Mask(y, K)
z_masked = tf.multiply(z, m)
z_hat, best_centroids = Q(z_masked, centroids, L, z)
z_differentiable = tf.stop_gradient(z_hat)


# Decoder / Generator

D_residual_blocks = []
D_residual_blocks.append(tf.layers.conv2d_transpose(inputs=z_differentiable,
                                                    filters=128,
                                                    kernel_size=[3, 3],
                                                    strides=(2, 2),
                                                    padding="same",
                                                    kernel_regularizer=regularizer,
                                                    name="conv"+str(depth*6+7)))

D_residual_blocks[-1] = tf.layers.batch_normalization(inputs=D_residual_blocks[-1], training=training)
D_residual_blocks[-1] = tf.nn.relu(D_residual_blocks[-1])
tmp = D_residual_blocks[-1]

for i in range(depth):
    tmp3 = tmp
    for j in range(3):
        tmp2 = tmp
        D_residual_blocks.append(tf.layers.conv2d(inputs=tmp,
                                                  filters=128,
                                                  kernel_size=[3, 3],
                                                  strides=(1, 1),
                                                  padding="same",
                                                  kernel_regularizer=regularizer,
                                                  name="conv"+str(6*i+2*j+depth*6+8)))

        D_residual_blocks[-1] = tf.layers.batch_normalization(inputs=D_residual_blocks[-1], training=training)
        D_residual_blocks[-1] = tf.nn.relu(D_residual_blocks[-1])
        tmp = D_residual_blocks[-1]
        D_residual_blocks.append(tf.layers.conv2d(inputs=tmp,
                                                  filters=128,
                                                  kernel_size=[3, 3],
                                                  strides=(1, 1),
                                                  padding="same",
                                                  kernel_regularizer=regularizer,
                                                  name="conv"+str(6*i+2*j+depth*6+9)))

        tmp = D_residual_blocks[-1] + tmp2
    tmp = tmp3 + tmp

tmp2 = tmp
D_residual_blocks.append(tf.layers.conv2d(inputs=tmp,
                                          filters=128,
                                          kernel_size=[3, 3],
                                          strides=(1, 1),
                                          padding="same",
                                          kernel_regularizer=regularizer,
                                          name="conv"+str(depth*14+4)))

D_residual_blocks[-1] = tf.layers.batch_normalization(inputs=D_residual_blocks[-1], training=training)
D_residual_blocks[-1] = tf.nn.relu(D_residual_blocks[-1])
tmp = D_residual_blocks[-1]
D_residual_blocks.append(tf.layers.conv2d(inputs=tmp,
                                          filters=128,
                                          kernel_size=[3, 3],
                                          strides=(1, 1),
                                          padding="same",
                                          kernel_regularizer=regularizer,
                                          name="conv"+str(depth*14+5)))

tmp = D_residual_blocks[-1] + tmp2 + D_residual_blocks[0]

deconv1 = tf.layers.conv2d_transpose(inputs=tmp,
                                     filters=64,
                                     kernel_size=[5, 5],
                                     strides=(2, 2),
                                     padding="same",
                                     kernel_regularizer=regularizer,
                                     name="deconv1")

deconv1 = tf.layers.batch_normalization(inputs=deconv1, training=training)
deconv1 = tf.nn.relu(deconv1)
deconv2 = tf.layers.conv2d_transpose(inputs=deconv1,
                                     filters=3,
                                     kernel_size=[5, 5],
                                     strides=(2, 2),
                                     padding="same",
                                     kernel_regularizer=regularizer,
                                     name="deconv2")

# Output Decoder

x_hat = deconv2


# Normalize Reconstructed Image


x_hat_norm = x_hat * tf.sqrt(var + 1e-10) + mean
x_hat_norm = tf.clip_by_value(x_hat_norm, 0, 1.0)


# Context Model

z_hat_contex_model = tf.stop_gradient(tf.expand_dims(z_differentiable, 4))

W_conv1 = get_weights('W_conv1', [3, 3, 3, 1, 24], mask_filter1)
b_conv1 = get_bias("b_conv1", [24])
conv1 = tf.nn.relu(tf.nn.bias_add(conv3d(z_hat_contex_model, W_conv1), b_conv1))

W_conv2 = get_weights('W_conv2', [3, 3, 3, 24, 24], mask_filter2)
b_conv2 = get_bias("b_conv2", [24])
conv2 = tf.nn.relu(tf.nn.bias_add(conv3d(conv1, W_conv2), b_conv2))

W_conv3 = get_weights('W_conv3', [3, 3, 3, 24, 24], mask_filter2)
b_conv3 = get_bias("b_conv3", [24])
conv3 = tf.nn.bias_add(conv3d(conv2, W_conv3), b_conv3)

W_conv4 = get_weights('W_conv4', [3, 3, 3, 24, n_centroids], mask_filter3)
b_conv4 = get_bias("b_conv4", [n_centroids])
conv4 = tf.nn.relu(tf.nn.bias_add(conv3d(conv3 + conv1, W_conv4), b_conv4))

P = tf.nn.softmax(conv4)







# Generator
Gz = x_hat_norm
s_gz = tf.summary.image("Gz", Gz, 1)
s_x_ae = tf.summary.image("x_ae", x_ae, 1)
s_x = tf.summary.image("x", x, 1)

g_acc = getMSSSIM(x, Gz)
ae_acc = getMSSSIM(x, x_ae)

tf.summary.scalar('ms-ssim_G', g_acc)
tf.summary.scalar('delta_ms-ssim', (g_acc - ae_acc))

Dx = discriminator(x, Dregularizer, DregularizerDense, batch_size)

Dg = discriminator(tf.stop_gradient(Gz), Dregularizer, DregularizerDense, batch_size, reuse_variables=True)

# losses

d_loss_real = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=Dx, labels=tf.ones_like(Dx)))
d_loss_fake = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=Dg, labels=tf.zeros_like(Dg)))
d_loss = d_loss_real + d_loss_fake
s_d_loss = tf.summary.scalar('loss_discriminator', d_loss)


g_loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=Dg, labels=tf.ones_like(Dg))) + g_acc * getAlpha(g_acc)
tf.summary.scalar('loss_generator', g_loss)

# Optimizers
optimizer_D = tf.train.AdamOptimizer(learning_rate=lrD)
optimizer_G = tf.train.AdamOptimizer(learning_rate=lrG)

update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
with tf.control_dependencies(update_ops):
    traind = optimizer_D.minimize(d_loss)
    traing = optimizer_G.minimize(g_loss)

training_init_op1 = iterator.make_initializer(dataset)

sess = tf.Session()

merged = tf.summary.merge_all()
train_writer2 = tf.summary.FileWriter('log_gan/discriminator', sess.graph)
train_writer3 = tf.summary.FileWriter('log_gan/adversarial', sess.graph)

init1 = tf.global_variables_initializer()
init2 = tf.local_variables_initializer()
sess.run(init1)
sess.run(init2)

saver = tf.train.Saver()

# Model Training ------------------------------------------------------------------------------------------------------------------

sess.run(training_init_op1)

saver.restore(sess, '/mnt/disks/disk2/ae/model.*')

# alleno discriminator da solo
num_batch = 1000
for e in range(epochsD):
    print("epoch: " + str(e) + " of " + str(epochsD))
    for i in range(num_batch):
        age = e * num_batch + i

        _, summary = sess.run((traind, s_d_loss))

        train_writer2.add_summary(summary, age)

# alleno GAN
num_batch = 4968
for e in range(epochsGAN):
    print("epoch: " + str(e) + " of " + str(epochsGAN))
    for i in range(num_batch):
        age = e * num_batch + i

        _, _, summary = sess.run((traind, traing, merged))  # forse anche g_acc

        train_writer3.add_summary(summary, age)

    if e % 2 == 1:
        lrD = tf.assign(lrD, lrD * 0.5)
        lrG = tf.assign(lrG, lrG * 0.5)

try:
    saver.save(sess, "/home/luca.marson1994/model/gan/model.ckpt")
    print("model saved successfully")
except Exception:
    pass
