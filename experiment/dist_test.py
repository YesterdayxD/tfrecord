from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import argparse
import json
import os
import shutil
import time

import numpy as np
import tensorflow as tf

parser = argparse.ArgumentParser()
parser.add_argument('--timeline', '-t', default='timeline')
parser.add_argument('--type', '-y', default='worker')
parser.add_argument('--index', '-i', default=0)
args = parser.parse_args()

if args.index=='0':
    model_dir = '/home/limk/saved_models_0/%s' % (args.timeline,)
    if os.path.exists(model_dir):
        shutil.rmtree(model_dir)
    os.makedirs(model_dir)
model_dir = '/home/limk/saved_models_0/%s' % (args.timeline,)
data_dir = '/data/train/tfdata/'
timeline_dir = '/home/limk/timeline/%s' % (args.timeline,)+str(args.index)
if os.path.exists(timeline_dir):
    shutil.rmtree(timeline_dir)
os.makedirs(timeline_dir, exist_ok=True)
#  TF_CONFIG
# On the parameter server:
# TF_CONFIG_JSON= {'cluster': {'chief': ['10.0.0.4:2224'], 'worker': ['10.0.0.4:2223', '10.0.0.5:2222', '10.0.0.6:2222'], 'ps': ['10.0.0.4:2222']}, 'task': {'type': 'ps', 'index': 0}, 'environment': 'cloud'}

# # On the chief
# TF_CONFIG_JSON= {'cluster': {'chief': ['10.0.0.4:2224'], 'worker': ['10.0.0.4:2223', '10.0.0.5:2222', '10.0.0.6:2222'], 'ps': ['10.0.0.4:2222']}, 'task': {'type': 'chief', 'index': 0}, 'environment': 'cloud'}
# # worker
# TF_CONFIG_JSON = {"cluster":{"chief":["172.172.0.2:2232"],"ps":["172.172.0.3:2233"],
#                      "worker":["172.172.0.4:2234"]},
# "task":{"type":args.type,"index":int(args.index)}}
TF_CONFIG_JSON = {"cluster": {
    "worker": ["172.172.0.2:2232", "172.172.0.3:2233", "172.172.0.4:2234"]},
    "task": {"type": args.type, "index": int(args.index)}}
os.environ['TF_CONFIG'] = json.dumps(TF_CONFIG_JSON)

# os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
if args.type == 'ps' or args.type == 'chief':
    os.environ["CUDA_VISIBLE_DEVICES"] = "0,1,2,3"
if args.type == 'worker':
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.index)

NUM_CLASSES = 1000
BATCH_SIZE = 256
EPOCHS = 1
NUM_GPUS = 1

TRAIN_SIZE = 1281167
'''
local meanstd = {
   mean = { 0.485, 0.456, 0.406 },
   std = { 0.229, 0.224, 0.225 },
}
'''


####### input pipeline 
def input_fn():
    # import pdb;pdb.set_trace()
    train_files_names = os.listdir(data_dir)
    train_files = [data_dir + item for item in train_files_names[:100]]
    dataset_train = tf.data.TFRecordDataset(train_files, buffer_size=2048,
                                            num_parallel_reads=128)

    def _parse_data(example_proto):
        # import pdb;pdb.set_trace()
        features = {'label': tf.FixedLenFeature([], tf.int64),
                    'img_raw': tf.FixedLenFeature([], tf.string)}
        parsed_features = tf.parse_single_example(example_proto, features)
        image = tf.decode_raw(parsed_features['img_raw'], tf.uint8)
        label = tf.cast(parsed_features['label'], tf.int64)
        label = tf.one_hot(label, depth=NUM_CLASSES)
        image = tf.reshape(image, [224, 224, 3])
        image = tf.cast(image, tf.float32) / 255
        return image, label

    dataset_train = dataset_train.repeat(EPOCHS)
    dataset_train = dataset_train.shuffle(buffer_size=1024)
    dataset_train = dataset_train.map(_parse_data, num_parallel_calls=30)
    dataset_train = dataset_train.batch(BATCH_SIZE)
    dataset_train = dataset_train.prefetch(2)
    return dataset_train


class TimeHistory(tf.train.SessionRunHook):
    def begin(self):
        self.times = []

    def before_run(self, run_context):
        self.iter_time_start = time.time()

    def after_run(self, run_context, run_values):
        self.times.append(time.time() - self.iter_time_start)


def main():
    tf.logging.set_verbosity(tf.logging.INFO)
    ### classifier
    model = tf.keras.applications.ResNet50(weights=None, classes=1000)
    model.summary()
    # import pdb;pdb.set_trace()

    optimizer = tf.train.AdamOptimizer(learning_rate=0.01)
    model.compile(loss=tf.keras.losses.categorical_crossentropy,
                  optimizer=optimizer, metrics=["accuracy"])

    time_hist = TimeHistory()
    # if NUM_GPUS==1:
    #   strategy = tf.contrib.distribute.OneDeviceStrategy(device='/device:GPU:2')
    # else:
    #   strategy = tf.contrib.distribute.MirroredStrategy(devices=['/device:GPU:2','/device:GPU:1'])
    # else: strategy = tf.contrib.distribute.MirroredStrategy(num_gpus=NUM_GPUS)
    # else:
    strategy = tf.contrib.distribute.CollectiveAllReduceStrategy(
        num_gpus_per_worker=1)  # need tensorflow 1.11 version

    session_config = tf.ConfigProto(gpu_options=tf.GPUOptions(
        allow_growth=True))  # , inter_op_parallelism_threads=2, intra_op_parallelism_threads=2)
    # session_config = None#tf.ConfigProto()
    # session_config.gpu_options.allow_growth = False

    config = tf.estimator.RunConfig(model_dir=model_dir,
                                    train_distribute=strategy,
                                    save_checkpoints_steps=5000,
                                    session_config=session_config)  # distributed mode, fixed batch_size and fixed memory using
    #                                  train_distribute=strategy, session_config = session_config, save_checkpoints_steps=5000)   # single mode, floating batch_size and memory using
    #                                  train_distribute=None,save_checkpoints_steps=5000)                                      # single mode, fixed batch_size and fixed memory using

    estimator = tf.keras.estimator.model_to_estimator(model, config=config)
    timeline = tf.train.ProfilerHook(save_steps=500, output_dir=timeline_dir)
    train_spec = tf.estimator.TrainSpec(input_fn=input_fn,
                                        hooks=[time_hist, timeline])
    eval_spec = tf.estimator.EvalSpec(input_fn=input_fn)
    tf.estimator.train_and_evaluate(estimator, train_spec, eval_spec)

    ####################################
    total_time = sum(time_hist.times)
    print(f"total time with {NUM_GPUS} GPU(s): {total_time} seconds")
    avg_time_per_batch = np.mean(time_hist.times)
    print(
        f"{BATCH_SIZE*NUM_GPUS/avg_time_per_batch} images/second with {NUM_GPUS} GPU(s)")


if __name__ == '__main__':
    main()
