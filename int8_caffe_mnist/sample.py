#
# Copyright (c) 2021, NVIDIA CORPORATION. All rights reserved.
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
#

import tensorrt as trt
import pycuda.driver as cuda
import pycuda.autoinit

import numpy as np
import random

# For our custom calibrator
from calibrator import load_mnist_data, load_mnist_labels, MNISTEntropyCalibrator

# For ../common.py
import sys, os
sys.path.insert(1, os.path.join(sys.path[0], os.path.pardir))
import common
#先创建trt.logger
TRT_LOGGER = trt.Logger()

#模型信息类，包括模型文件的路径以及输出结点的名称等
class ModelData(object):
    DEPLOY_PATH = "deploy.prototxt"
    MODEL_PATH = "mnist_lenet.caffemodel"
    OUTPUT_NAME = "prob"
    # The original model is a float32 one.
    DTYPE = trt.float32


# This function builds an engine from a Caffe model.
#创建一个int8的推理引擎
def build_int8_engine(deploy_file, model_file, calib, batch_size=32):
    #创建相关的实例
    with trt.Builder(TRT_LOGGER) as builder, builder.create_network() as network, builder.create_builder_config() as config, trt.CaffeParser() as parser:
        # We set the builder batch size to be the same as the calibrator's, as we use the same batches
        # during inference. Note that this is not required in general, and inference batch size is
        # independent of calibration batch size.
        #指定相关的参数
        builder.max_batch_size = batch_size
        config.max_workspace_size = common.GiB(1)
        config.set_flag(trt.BuilderFlag.INT8)
        config.int8_calibrator = calib
        # Parse Caffe model
        #使用caffe解析器解析模型，解析一个prototxt文件和一个binaryproto Caffe模型，分别提取网络定义和与网络相关的权值。
        model_tensors = parser.parse(deploy=deploy_file, model=model_file, network=network, dtype=ModelData.DTYPE)
        #标记网络的输出
        network.mark_output(model_tensors.find(ModelData.OUTPUT_NAME))
        # Build engine and do int8 calibration.
        #构建相应的引擎
        return builder.build_engine(network, config)

#获取相应的tensorrt的推理准确率
def check_accuracy(context, batch_size, test_set, test_labels):
    #给相应的引擎分配缓冲区，用来保存主机和设备的输入输出数据等，返回分配的缓冲区的指针列表
    inputs, outputs, bindings, stream = common.allocate_buffers(context.engine)

    num_correct = 0
    num_total = 0

    batch_num = 0
    #开始提取推理数据
    for start_idx in range(0, test_set.shape[0], batch_size):
        batch_num += 1
        if batch_num % 10 == 0:
            print("Validating batch {:}".format(batch_num))
        # If the number of images in the test set is not divisible by the batch size, the last batch will be smaller.
        # This logic is used for handling that case.
        #定位一个batch_size的结尾
        end_idx = min(start_idx + batch_size, test_set.shape[0])
        #确定有效的数据量，通常只有最后一个batch可能会少一些
        effective_batch_size = end_idx - start_idx

        # Do inference for every batch.
        #对每一个batch的数据进行推理
        #将相应的数据赋值到筑基内存上
        inputs[0].host = test_set[start_idx:start_idx + effective_batch_size]
        #进行具体的推理，参考common.py的具体实现
        [output] = common.do_inference(context, bindings=bindings, inputs=inputs, outputs=outputs, stream=stream, batch_size=effective_batch_size)

        # Use argmax to get predictions and then check accuracy
        #获取最终的预测输出
        preds = np.argmax(output.reshape(32, 10)[0:effective_batch_size], axis=1)
        labels = test_labels[start_idx:start_idx + effective_batch_size]
        num_total += effective_batch_size
        num_correct += np.count_nonzero(np.equal(preds, labels))

    percent_correct = 100 * num_correct / float(num_total)
    print("Total Accuracy: {:}%".format(percent_correct))


def main():
    #解析样本数据得到相应的数据文件路径
    _, data_files = common.find_sample_data(description="Runs a Caffe MNIST network in Int8 mode", subfolder="mnist", find_files=["t10k-images-idx3-ubyte", "t10k-labels-idx1-ubyte", "train-images-idx3-ubyte", ModelData.DEPLOY_PATH, ModelData.MODEL_PATH], err_msg="Please follow the README to download the MNIST dataset")
    #给相应参数赋值
    [test_set, test_labels, train_set, deploy_file, model_file] = data_files

    # Now we create a calibrator and give it the location of our calibration data.
    # We also allow it to cache calibration data for faster engine building.
    #创建一个校准类实例
    calibration_cache = "mnist_calibration.cache"
    #MNISTEntropyCalibrator参考calibrator.py中的实现
    calib = MNISTEntropyCalibrator(test_set, cache_file=calibration_cache)

    # Inference batch size can be different from calibration batch size.
    #推理的batch_size跟校准的batch_size可以不一样
    batch_size = 32
    #build_int8_engine参考本文件下的实现
    with build_int8_engine(deploy_file, model_file, calib, batch_size) as engine, engine.create_execution_context() as context:
        # Batch size for inference can be different than batch size used for calibration.
        #check_accuracy参考本文件下的实现
        check_accuracy(context, batch_size, test_set=load_mnist_data(test_set), test_labels=load_mnist_labels(test_labels))

if __name__ == '__main__':
    main()
