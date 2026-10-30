# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Google Coral Edge TPU backend (USB or PCIe/M.2).

Default model: Google's SSDLite MobileDet COCO (320x320, uint8), the one
Frigate ships -- decode and NMS run in-graph (TFLite_Detection_PostProcess).
An Ultralytics YOLOv5 full-int8 export compiled with edgetpu_compiler (one
[1,N,85] output, 0-1 normalized boxes) also loads; the layout is detected from
the outputs. Runs via ai-edge-litert (or the older tflite-runtime) with the
libedgetpu delegate; pycoral is not needed.
"""
import glob
import logging
import os

import numpy as np

from . import postprocess
from .base import Detector

log = logging.getLogger("aiport-detector")

# Coral USB enumerates as 1a6e:089a until libedgetpu loads its firmware, then 18d1:9302.
_USB_IDS = {("1a6e", "089a"), ("18d1", "9302")}
_PCIE_DEVICES = ("/dev/apex_0",)
_DELEGATE_LIB = "libedgetpu.so.1"


def _usb_coral_present():
    for dev in glob.glob("/sys/bus/usb/devices/*"):
        try:
            with open(os.path.join(dev, "idVendor")) as f:
                vendor = f.read().strip()
            with open(os.path.join(dev, "idProduct")) as f:
                product = f.read().strip()
        except OSError:
            continue
        if (vendor, product) in _USB_IDS:
            return True
    return False


def _tpu_device():
    """libedgetpu device spec ("usb"/"pci") for the first Coral found, or None."""
    if _usb_coral_present():
        return "usb"
    if any(os.path.exists(p) for p in _PCIE_DEVICES):
        return "pci"
    return None


def _tflite_api():
    """(Interpreter, load_delegate) from ai-edge-litert, else tflite-runtime."""
    try:
        from ai_edge_litert.interpreter import Interpreter, load_delegate
    except ImportError:
        from tflite_runtime.interpreter import Interpreter, load_delegate
    return Interpreter, load_delegate


def _ssd_output_order(outputs):
    """Indices (boxes, classes, scores, count) of TFLite_Detection_PostProcess-style
    outputs, or None if the model isn't one. boxes is the [1,N,4] tensor and
    count the 1-D one; TFLite_Detection_PostProcess emits classes before scores,
    which its ":1"/":2" name suffixes (or, failing that, the output order) give."""
    if len(outputs) != 4:
        return None
    shapes = [tuple(o["shape"]) for o in outputs]
    try:
        boxes = next(i for i, s in enumerate(shapes) if len(s) == 3 and s[-1] == 4)
        count = next(i for i, s in enumerate(shapes) if len(s) == 1)
    except StopIteration:
        return None
    pair = [i for i in range(4) if i not in (boxes, count)]
    if any(len(shapes[i]) != 2 for i in pair):
        return None
    if all(outputs[i]["name"].startswith("TFLite_Detection_PostProcess") for i in pair):
        pair.sort(key=lambda i: outputs[i]["name"])
    return boxes, pair[0], pair[1], count


class EdgeTpuDetector(Detector):
    NAME = "edgetpu"
    DEFAULT_MODEL = os.path.join(postprocess.MODELS_DIR,
                                 "ssdlite_mobiledet_coco_qat_postprocess_edgetpu.tflite")

    @classmethod
    def available(cls, model_path=None):
        try:
            _tflite_api()
        except ImportError:
            return False, "neither ai-edge-litert nor tflite-runtime installed"
        if _tpu_device() is None:
            return False, "no Coral Edge TPU found (USB or /dev/apex_0)"
        model = model_path or cls.DEFAULT_MODEL
        if not os.path.exists(model):
            return False, f"model missing: {model}"
        return True, ""

    def __init__(self, model_path=None):
        super().__init__(model_path)
        try:
            Interpreter, load_delegate = _tflite_api()
        except ImportError as exc:
            raise RuntimeError(
                "ai-edge-litert (or tflite-runtime) is not installed -- see "
                "diyport/README.md for the Edge TPU setup") from exc
        device = _tpu_device()
        options = {"device": device} if device else {}
        try:
            delegate = load_delegate(_DELEGATE_LIB, options)
        except (ValueError, OSError) as exc:
            # A Coral wedged by an earlier failed open times out here until it's replugged.
            raise RuntimeError(
                f"could not load {_DELEGATE_LIB} for Edge TPU ({device}): {exc} -- "
                "if the Coral was just plugged in or a load failed before, replug it") from exc
        self._interp = Interpreter(model_path=self.model_path, experimental_delegates=[delegate])
        self._interp.allocate_tensors()
        self._in = self._interp.get_input_details()[0]
        self.input_size = int(self._in["shape"][1])  # NHWC, square
        outs = self._interp.get_output_details()
        self._ssd = _ssd_output_order(outs)
        if self._ssd is not None:
            self._outs = outs
            self._labels = postprocess.load_labels(postprocess.LABELS_90_PATH)
            layout = "ssd"
        elif len(outs) == 1 and len(outs[0]["shape"]) == 3 and outs[0]["shape"][-1] == 85:
            self._outs = outs
            layout = "yolo"
        else:
            raise RuntimeError(
                f"unsupported Edge TPU model outputs {[o['shape'].tolist() for o in outs]}; "
                "expected TFLite_Detection_PostProcess (SSD/MobileDet) or YOLOv5 [1,N,85]")
        log.info("Edge TPU detector initialized (%s, device=%s, layout=%s, input=%d, "
                 "obj_thresh=%.2f)", self.model_path, device, layout, self.input_size,
                 postprocess.OBJ_THRESH)

    def _prepare_input(self, frame_rgb):
        """Raw pixels in the model's input dtype. Edge TPU models take uint8
        pixels as-is (the input quantization maps them to whatever range the
        model was trained on); an int8 input is the same pixels shifted by 128."""
        dtype = self._in["dtype"]
        if dtype == np.int8:
            return (frame_rgb.astype(np.int16) - 128).astype(np.int8)[np.newaxis, ...]
        if dtype == np.float32:
            return (frame_rgb.astype(np.float32) / 255.0)[np.newaxis, ...]
        return frame_rgb.astype(dtype, copy=False)[np.newaxis, ...]

    def _output(self, detail):
        raw = self._interp.get_tensor(detail["index"])
        scale, zero = detail["quantization"]
        if scale == 0 or raw.dtype == np.float32:
            return raw.astype(np.float32, copy=False)
        return (raw.astype(np.float32) - zero) * scale

    def _infer(self, frame_rgb):
        self._interp.set_tensor(self._in["index"], self._prepare_input(frame_rgb))
        self._interp.invoke()
        if self._ssd is not None:
            boxes, classes, scores, count = (self._output(self._outs[i]) for i in self._ssd)
            return postprocess.post_process_ssd(boxes, classes, scores, count, self.input_size)
        output = self._output(self._outs[0])
        return postprocess.post_process_ultralytics(output, box_scale=self.input_size)
