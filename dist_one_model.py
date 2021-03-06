import os

import tensorflow as tf

# TODO(limk):将本文件改为分布式，查看读取数据的顺序

print(tf.__version__)
filepath = "./image"

abs_path = os.path.dirname(os.path.abspath(__file__))
print(os.path.abspath(__file__))
print('当前目录绝对路径:', abs_path + '/image')
image_dir = abs_path + '/image'

img_name_list = os.listdir(image_dir)
print(img_name_list)

all_image_paths = [image_dir + '/' + img_name for img_name in img_name_list]
print(all_image_paths)

path_ds = tf.data.Dataset.from_tensor_slices(all_image_paths)

print('shape: ', repr(path_ds.output_shapes))
print('type: ', path_ds.output_types)
print()
print(path_ds)


# img_raw = tf.gfile.FastGFile(filepath + '/' + img_path, mode='rb').read()
# print(repr(img_raw)[:100] + "...")


def preprocess_image(image):
    image = tf.image.decode_jpeg(image, channels=3)
    image = tf.image.resize_images(image, [224, 224])
    image /= 255.0
    return image


def load_and_preprocess_image(path):
    """

    :param path:the path of an image
    :return: a image tensor
    """
    image = tf.read_file(path)
    return preprocess_image(image)


image_ds = path_ds.map(load_and_preprocess_image)
print(image_ds)

all_image_labels = [i for i in range(0, 9)]
label_ds = tf.data.Dataset.from_tensor_slices(
    tf.cast(all_image_labels, tf.int64))
print(label_ds)

ds = tf.data.Dataset.from_tensor_slices((all_image_paths, all_image_labels))
print(ds)


# The tuples are unpacked into the positional arguments of the mapped function
def load_and_preprocess_from_path_label(path, label):
    return load_and_preprocess_image(path), label


image_label_ds = ds.map(load_and_preprocess_from_path_label)

BATCH_SIZE = 1

# Setting a shuffle buffer size as large as the dataset ensures that the data is
# completely shuffled.
ds = image_label_ds.shuffle(buffer_size=len(all_image_labels), seed=1)
ds = ds.repeat(100)
ds = ds.batch(BATCH_SIZE)
# `prefetch` lets the dataset fetch batches, in the background while the model is training.
print(ds)

iterator = ds.make_one_shot_iterator()

x_iter, y_iter = iterator.get_next()

# Flags for defining the tf.train.ClusterSpec
tf.app.flags.DEFINE_string("ps_hosts", "172.172.0.2:2232",
                           "Comma-separated list of hostname:port pairs")
tf.app.flags.DEFINE_string("worker_hosts", "172.172.0.3:2233,172.172.0.4:2234",
                           "Comma-separated list of hostname:port pairs")

# Flags for defining the tf.train.Server
tf.app.flags.DEFINE_string("job_name", None, "One of 'ps', 'worker'")
tf.app.flags.DEFINE_integer("task_index", None, "Index of task within the job")
tf.app.flags.DEFINE_integer("train_steps", 900, "train_steps of the job")

FLAGS = tf.app.flags.FLAGS

NUM_CLASSES = 10


def main(_):
    ps_hosts = FLAGS.ps_hosts.split(",")
    worker_hosts = FLAGS.worker_hosts.split(",")

    num_workers = len(worker_hosts)

    cluster = tf.train.ClusterSpec({"ps": ps_hosts, "worker": worker_hosts})

    gpu_config = tf.ConfigProto(allow_soft_placement=True,
                                log_device_placement=False)
    gpu_config.gpu_options.allow_growth = True

    server = tf.train.Server(
        cluster, job_name=FLAGS.job_name, task_index=FLAGS.task_index,
        config=gpu_config)

    if FLAGS.job_name == "ps":
        server.join()
    elif FLAGS.job_name == "worker":
        is_chief = (FLAGS.task_index == 0)
        with tf.device(tf.train.replica_device_setter(
                worker_device="/job:worker/task:%d" % FLAGS.task_index,
                cluster=cluster)):
            x = tf.placeholder(tf.float32, [None, 224, 224, 3])
            y = tf.placeholder(tf.int64, [None])

            conv0 = tf.layers.Conv2D(filters=4, kernel_size=5)(x)

            flatten = tf.layers.Flatten()(conv0)

            y_ = tf.layers.Dense(10, activation=tf.nn.relu)(flatten)

            y_ = tf.nn.softmax(y_)

            losses = tf.nn.softmax_cross_entropy_with_logits(logits=y_,
                                                             labels=tf.one_hot(
                                                                 y,
                                                                 depth=NUM_CLASSES))

            mean_loss = tf.reduce_mean(losses)

            # global_step = tf.Variable(0)

            # global_step = tf.contrib.framework.get_or_create_global_step()
            global_step = tf.train.get_or_create_global_step()

            optsync = tf.train.SyncReplicasOptimizer(
                tf.train.AdamOptimizer(learning_rate=1e-2),
                replicas_to_aggregate=num_workers,
                total_num_replicas=num_workers,
                use_locking=True
            )

            sync_replicas_hook = optsync.make_session_run_hook(is_chief)
            hooks = [sync_replicas_hook, tf.train.StopAtStepHook(
                last_step=FLAGS.train_steps)]
            train_op = optsync.minimize(mean_loss, global_step=global_step)

            init_op = tf.global_variables_initializer()

            with tf.train.MonitoredTrainingSession(master=server.target,
                                                   is_chief=(
                                                           FLAGS.task_index == 0),
                                                   # checkpoint_dir=FLAGS.log_dir,
                                                   # save_checkpoint_secs=60
                                                   hooks=hooks,
                                                   config=gpu_config,
                                                   stop_grace_period_secs=30
                                                   ) as mon_sess:
                # 用于保存和载入模型
                # log_dir = FLAGS.log_dir
                mon_sess.run(init_op)
                if is_chief:
                    print(
                        'Worker %d: Initailizing session...' % FLAGS.task_index)
                else:
                    print(
                        'Worker %d: Waiting for session to be initaialized...' %
                        FLAGS.task_index)
                print(
                    'Worker %d: Session initialization complete.' % FLAGS.task_index)

                local_step = 0
                while not mon_sess.should_stop():
                    print("labels: {}".format(mon_sess.run(y_iter)))

                    _, loss_, step = mon_sess.run(
                        [train_op, mean_loss, global_step],
                        feed_dict={x: mon_sess.run(x_iter),
                                   y: mon_sess.run(
                                       y_iter)})

                    local_step += 1
                    print("local_step: {}".format(local_step))
                    print("global_step: {}".format(step))
                    print("loss_: {}".format(loss_))


if __name__ == "__main__":
    tf.app.run()
