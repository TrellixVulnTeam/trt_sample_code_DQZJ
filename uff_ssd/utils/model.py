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

# Model download and UFF convertion utils
import os
import sys
import tarfile

import requests
import tensorflow as tf
import tensorrt as trt
import graphsurgeon as gs
import uff
import time
import math

from utils.paths import PATHS

sys.path.insert(1, os.path.join(sys.path[0], os.path.pardir))
from common import retry

# UFF conversion functionality

# This class contains converted (UFF) model metadata
#包含模型的相关信息类
class ModelData(object):
    # Name of input node
    INPUT_NAME = "Input"
    # CHW format of model input
    INPUT_SHAPE = (3, 300, 300)
    # Name of output node
    OUTPUT_NAME = "NMS"

    @staticmethod
    def get_input_channels():
        return ModelData.INPUT_SHAPE[0]

    @staticmethod
    def get_input_height():
        return ModelData.INPUT_SHAPE[1]

    @staticmethod
    def get_input_width():
        return ModelData.INPUT_SHAPE[2]

#调整相应的计算图，修改tensorrt不支持的层次，利用自定义插件层来代替
def ssd_unsupported_nodes_to_plugin_nodes(ssd_graph):
    """Makes ssd_graph TensorRT comparible using graphsurgeon.

    This function takes ssd_graph, which contains graphsurgeon
    DynamicGraph data structure. This structure describes frozen Tensorflow
    graph, that can be modified using graphsurgeon (by deleting, adding,
    replacing certain nodes). The graph is modified by removing
    Tensorflow operations that are not supported by TensorRT's UffParser
    and replacing them with custom layer plugin nodes.

    Note: This specific implementation works only for
    ssd_inception_v2_coco_2017_11_17 network.

    Args:
        ssd_graph (gs.DynamicGraph): graph to convert
    Returns:
        gs.DynamicGraph: UffParser compatible SSD graph
    """
    # Create TRT plugin nodes to replace unsupported ops in Tensorflow graph
    #获取相应的输入节点的维度
    channels = ModelData.get_input_channels()
    height = ModelData.get_input_height()
    width = ModelData.get_input_width()
    #创建一个新的插件节点作为输入节点
    #create_plugin_node参考https://docs.nvidia.com/deeplearning/tensorrt/api/python_api/graphsurgeon/graphsurgeon.html?highlight=create_plugin_node#graphsurgeon.create_plugin_node
    Input = gs.create_plugin_node(name="Input",
        op="Placeholder",
        dtype=tf.float32,
        shape=[1, channels, height, width])
    #创建priorBox插件节点
    PriorBox = gs.create_plugin_node(name="GridAnchor", op="GridAnchor_TRT",
        minSize=0.2,
        maxSize=0.95,
        aspectRatios=[1.0, 2.0, 0.5, 3.0, 0.33],
        variance=[0.1,0.1,0.2,0.2],
        featureMapShapes=[19, 10, 5, 3, 2, 1],
        numLayers=6
    )
    #创建NMS插件节点
    NMS = gs.create_plugin_node(
        name="NMS",
        op="NMS_TRT",
        shareLocation=1,
        varianceEncodedInTarget=0,
        backgroundLabelId=0,
        confidenceThreshold=1e-8,
        nmsThreshold=0.6,
        topK=100,
        keepTopK=100,
        numClasses=91,
        inputOrder=[0, 2, 1],
        confSigmoid=1,
        isNormalized=1
    )
    #create_node参考https://docs.nvidia.com/deeplearning/tensorrt/api/python_api/graphsurgeon/graphsurgeon.html?highlight=create_node#graphsurgeon.create_node
    concat_priorbox = gs.create_node(
        "concat_priorbox",
        op="ConcatV2",
        dtype=tf.float32,
        axis=2
    )

    concat_box_loc = gs.create_plugin_node(
        "concat_box_loc",
        op="FlattenConcat_TRT",
        dtype=tf.float32,
        axis=1,
        ignoreBatch=0
    )

    concat_box_conf = gs.create_plugin_node(
        "concat_box_conf",
        op="FlattenConcat_TRT",
        dtype=tf.float32,
        axis=1,
        ignoreBatch=0
    )

    # Create a mapping of namespace names -> plugin nodes.
    #创建一个命名空间完成到插件节点的映射，实际上就是将相应的层次注册成一个自定义插件，实现相应的操作
    namespace_plugin_map = {
        "MultipleGridAnchorGenerator": PriorBox,
        "Postprocessor": NMS,
        "Preprocessor": Input,
        "ToFloat": Input,
        "image_tensor": Input,
        "MultipleGridAnchorGenerator/Concatenate": concat_priorbox,
        "MultipleGridAnchorGenerator/Identity": concat_priorbox,
        "concat": concat_box_loc,
        "concat_1": concat_box_conf
    }

    # Create a new graph by collapsing namespaces
    #将相应的命名空间中的节点折叠成用户指定的单个节点
    ssd_graph.collapse_namespaces(namespace_plugin_map)
    # Remove the outputs, so we just have a single output node (NMS).
    # If remove_exclusive_dependencies is True, the whole graph will be removed!
    #从图中删除相应的节点，不转发输入，这里删除相应的output节点
    ssd_graph.remove(ssd_graph.graph_outputs, remove_exclusive_dependencies=False)
    return ssd_graph
#将pb模型转换到uff并保存到文件
def model_to_uff(model_path, output_uff_path, silent=False):
    """Takes frozen .pb graph, converts it to .uff and saves it to file.

    Args:
        model_path (str): .pb model path
        output_uff_path (str): .uff path where the UFF file will be saved
        silent (bool): if False, writes progress messages to stdout

    """
    #获取相应的动态图
    #DynamicGraph可以搜索和修改一个tensorflow GraphDef
    dynamic_graph = gs.DynamicGraph(model_path)
    #ssd_unsupported_nodes_to_plugin_nodes参考本文件下的实现
    #修改相应的计算图，用自定义插件代替tensorrt中不支持的图层
    dynamic_graph = ssd_unsupported_nodes_to_plugin_nodes(dynamic_graph)
    #完成相应的转换
    uff.from_tensorflow(
        dynamic_graph.as_graph_def(),
        [ModelData.OUTPUT_NAME],
        output_filename=output_uff_path,
        text=True
    )


# Model download functionality

def maybe_print(should_print, print_arg):
    """Prints message if supplied boolean flag is true.

    Args:
        should_print (bool): if True, will print print_arg to stdout
        print_arg (str): message to print to stdout
    """
    if should_print:
        print(print_arg)

def maybe_mkdir(dir_path):
    """Makes directory if it doesn't exist.

    Args:
        dir_path (str): directory path
    """
    if not os.path.exists(dir_path):
        os.makedirs(dir_path)


def download_file(file_url, file_dest_path, silent=False):
    """Downloads file from supplied URL and puts it into supplied directory.

    Args:
        file_url (str): URL with file to download
        file_dest_path (str): path to save downloaded file in
        silent (bool): if False, writes progress messages to stdout
    """

    @retry(n_retries=3)
    def _download_file(file_url, file_dest, silent=False):
        response = requests.get(file_url, stream=True)
        total_length = response.headers.get('content-length')

        def print_progress(pct_done):
            isatty = sys.stdout.isatty()
            clear_char = "\r" if isatty else ""
            endl_char = "" if isatty else "\n"
            progress_bar_width = int(math.floor(pct_done * 50 / 100.0))
            sys.stdout.write("{}Download progress [{}{}] {:.2f}%{}".format(
                  clear_char,
                  "=" * progress_bar_width,
                  " " * (50 - progress_bar_width),
                  pct_done,
                  endl_char))
            sys.stdout.flush()

        if total_length is None or silent: # no content length header or silent, just write file
            f.write(response.content)
        else: # not silent, print progress
            dl = 0
            total_length = int(total_length)
            t_last_update = t_cur = time.time()
            for data in response.iter_content(chunk_size=(4096 * 1024)):
                dl += len(data)
                file_dest.write(data)
                if t_cur - t_last_update > 2.0:
                    print_progress(100 * dl / total_length)
                    t_last_update = t_cur
                t_cur = time.time()
            print_progress(100)
            sys.stdout.write("\n")

    with open(file_dest_path, "wb") as f:
        maybe_print(not silent, "Downloading {}".format(file_dest_path))
        _download_file(file_url, f, silent=silent)
#download_model(model_name, silent)
def download_model(model_name, silent=False):
    """Downloads model_name from Tensorflow model zoo.

    Args:
        model_name (str): chosen object detection model
        silent (bool): if False, writes progress messages to stdout
    """
    maybe_print(not silent, "Preparing pretrained model")
    model_dir = PATHS.get_models_dir_path()
    maybe_mkdir(model_dir)
    model_url = PATHS.get_model_url(model_name)
    model_archive_path = os.path.join(model_dir, "{}.tar.gz".format(model_name))
    download_file(model_url, model_archive_path, silent=True)
    maybe_print(not silent, "Download complete\nUnpacking {}".format(model_archive_path))
    with tarfile.open(model_archive_path, "r:gz") as tar:
        def is_within_directory(directory, target):
            
            abs_directory = os.path.abspath(directory)
            abs_target = os.path.abspath(target)
        
            prefix = os.path.commonprefix([abs_directory, abs_target])
            
            return prefix == abs_directory
        
        def safe_extract(tar, path=".", members=None, *, numeric_owner=False):
        
            for member in tar.getmembers():
                member_path = os.path.join(path, member.name)
                if not is_within_directory(path, member_path):
                    raise Exception("Attempted Path Traversal in Tar File")
        
            tar.extractall(path, members, numeric_owner=numeric_owner) 
            
        
        safe_extract(tar, path=model_dir)
    maybe_print(not silent, "Extracting complete\nRemoving {}".format(model_archive_path))
    os.remove(model_archive_path)
    maybe_print(not silent, "Model ready")
#解析相应的模型并且转换到uff
def prepare_ssd_model(model_name="ssd_inception_v2_coco_2017_11_17", silent=False):
    """Downloads pretrained object detection model and converts it to UFF.

    The model is downloaded from Tensorflow object detection model zoo.
    Currently only ssd_inception_v2_coco_2017_11_17 model is supported
    due to model_to_uff() using logic specific to that network when converting.

    Args:
        model_name (str): chosen object detection model
        silent (bool): if False, writes progress messages to stdout
    """
    if model_name != "ssd_inception_v2_coco_2017_11_17":
        raise NotImplementedError(
            "Model {} is not supported yet".format(model_name))
    #download_model参考本文件的实现
    #下载相关的预训练模型
    download_model(model_name, silent)
    ssd_pb_path = PATHS.get_model_pb_path(model_name)
    ssd_uff_path = PATHS.get_model_uff_path(model_name)
    #完成从pb模型到uff模型的转换
    #model_to_uff参考本文件下的实现
    model_to_uff(ssd_pb_path, ssd_uff_path, silent)
