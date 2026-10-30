# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""RK3588 NPU backend.

Model: YOLOv5s (ReLU) INT8, converted from Rockchip's rknn_model_zoo v2.3.2
with rknn-toolkit2 on an x86_64 host. Runs via rknn-toolkit-lite2 against the
board's librknnrt.so.
"""
import logging
import os

import numpy as np

from . import postprocess
from .base import Detector

log = logging.getLogger("aiport-detector")

NPU_DEVICE_PATHS = ("/dev/rknpu", "/sys/kernel/debug/rknpu", "/proc/device-tree/npu@fdab0000")


class RknnDetector(Detector):
    NAME = "rknn"
    DEFAULT_MODEL = os.path.join(postprocess.MODELS_DIR, "yolov5s_relu.rknn")

    @classmethod
    def available(cls, model_path=None):
        try:
            import rknnlite.api  # noqa: F401
        except ImportError:
            return False, "rknn-toolkit-lite2 not installed"
        if not any(os.path.exists(p) for p in NPU_DEVICE_PATHS):
            return False, "no RKNPU found"
        model = model_path or cls.DEFAULT_MODEL
        if not os.path.exists(model):
            return False, f"model missing: {model}"
        return True, ""

    def __init__(self, model_path=None):
        super().__init__(model_path)
        try:
            from rknnlite.api import RKNNLite
        except ImportError as exc:
            raise RuntimeError(
                "rknnlite is not installed -- install rknn-toolkit-lite2 on the device "
                "(see requirements.txt)") from exc
        self.input_size = 640  # the model's fixed input
        self._anchors = postprocess.load_anchors()
        self._rknn = RKNNLite()
        ret = self._rknn.load_rknn(self.model_path)
        if ret != 0:
            raise RuntimeError(f"load_rknn failed: {ret}")
        ret = self._rknn.init_runtime()
        if ret != 0:
            raise RuntimeError(f"init_runtime failed: {ret}")
        log.info("RKNN detector initialized (%s)", self.model_path)

    def _infer(self, frame_rgb):
        # RKNNLite needs an explicit batch dim: (1,H,W,3), not (H,W,3).
        outputs = self._rknn.inference(inputs=[frame_rgb[np.newaxis, ...]])
        size = (self.input_size, self.input_size)
        return postprocess.post_process_raw_heads(outputs, self._anchors, size)
