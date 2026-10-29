# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
# ONNX->RKNN recipe adapted from Rockchip's rknn_model_zoo (examples/yolov5), Apache-2.0.
import sys
from rknn.api import RKNN

DATASET_PATH = './coco_subset_20.txt'

model_path = sys.argv[1]
platform = sys.argv[2]
output_path = sys.argv[3]

rknn = RKNN(verbose=True)

print('--> Config model')
rknn.config(mean_values=[[0, 0, 0]], std_values=[[255, 255, 255]], target_platform=platform)
print('done')

print('--> Loading model')
ret = rknn.load_onnx(model=model_path)
if ret != 0:
    print('Load model failed!')
    exit(ret)
print('done')

print('--> Building model')
ret = rknn.build(do_quantization=True, dataset=DATASET_PATH)
if ret != 0:
    print('Build model failed!')
    exit(ret)
print('done')

print('--> Export rknn model')
ret = rknn.export_rknn(output_path)
if ret != 0:
    print('Export rknn model failed!')
    exit(ret)
print('done')

rknn.release()
