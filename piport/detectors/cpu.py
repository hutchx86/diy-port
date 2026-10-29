# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""CPU backend (no accelerator): ONNX model via onnxruntime.

Takes an Ultralytics YOLOv5 ONNX export (one [1,N,85] output, default
models/yolov5n.onnx), Rockchip's yolov5s_relu.onnx (three raw heads) or an
anchor-free YOLOv8/v9/v11 export ([1,84,N], e.g. Frigate's yolov9-s-320.onnx);
the layout and input size are read from the graph. On Apple silicon, onnxruntime's Core ML
provider runs the model on the Neural Engine (~5-6x the CPU path); Linux and
Docker builds only offer CPU. Slow next to an NPU/TPU -- fine for one or two
cameras at the 1.5s detection tick.
"""
import logging
import os

import numpy as np

from . import postprocess
from .base import Detector

log = logging.getLogger("aiport-detector")


# Where Core ML runs the model (AIPORT_COREML_UNITS): auto leaves Core ML's choice (the Neural
# Engine for these models); gpu keeps it off the Neural Engine, which Frigate's Core ML
# detector shares on the same Mac (live 18:40 ANE power rose ~7x with every camera's pass time
# while the CPU sat half idle).
COREML_UNITS = {"ane": "CPUAndNeuralEngine", "gpu": "CPUAndGPU", "cpu": "CPUOnly", "all": "ALL"}


def _onnx_providers(available, fp16_input=True):
    """onnxruntime providers (AIPORT_ORT_PROVIDER = auto/coreml/cpu): Core ML on
    macOS (~5-6x CPU on Apple silicon), else CPU. The Core ML format follows the
    model's input dtype; on an M2 each format was ~7x slower on the other model."""
    want = os.environ.get("AIPORT_ORT_PROVIDER", "auto").strip().lower()
    if want != "cpu" and "CoreMLExecutionProvider" in available:
        if fp16_input:
            # Default NeuralNetwork format falls back to CPU on the fp16 Ultralytics
            # YOLOv5 (95 ms vs 4 ms per frame).
            opts = {"ModelFormat": "MLProgram", "MLComputeUnits": "CPUAndNeuralEngine"}
        else:
            # float32 graphs (Frigate's yolov9-s-320): NeuralNetwork 3 ms, MLProgram 22 ms.
            opts = {}
        units = COREML_UNITS.get(os.environ.get("AIPORT_COREML_UNITS", "auto").strip().lower())
        if units:
            opts = dict(opts, MLComputeUnits=units)
        if units == "CPUAndGPU":
            # On the GPU the NeuralNetwork format ran yolov9-s-320 at 22 ms, MLProgram at 7 ms (M2).
            opts["ModelFormat"] = "MLProgram"
        return [("CoreMLExecutionProvider", opts), "CPUExecutionProvider"]
    if want == "coreml":
        log.warning("AIPORT_ORT_PROVIDER=coreml but onnxruntime has no Core ML provider; using CPU")
    return ["CPUExecutionProvider"]


def _session_options(ort):
    """Thread/parallelism tuning shared by CPU and Core ML sessions."""
    so = ort.SessionOptions()
    # YOLOv5 is one sequential graph: no inter-op parallelism needed.
    so.inter_op_num_threads = 1
    intra = os.environ.get("AIPORT_ORT_INTRA_THREADS")
    if intra:
        so.intra_op_num_threads = int(intra)  # 0 = auto (all cores)
    # No spin-wait between inferences: onnxruntime's default spin burned ~3
    # extra cores on a 3-camera Pi 4 for no throughput. Park threads instead.
    for key in ("session.intra_op.allow_spinning", "session.inter_op.allow_spinning"):
        try:
            so.add_session_config_entry(key, "0")
        except Exception:
            pass
    return so


class CpuDetector(Detector):
    NAME = "cpu"
    DEFAULT_MODEL = os.path.join(postprocess.MODELS_DIR, "yolov5n.onnx")

    @classmethod
    def available(cls, model_path=None):
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return False, "onnxruntime not installed"
        model = model_path or cls.DEFAULT_MODEL
        if not os.path.exists(model):
            return False, f"model missing: {model}"
        return True, ""

    def __init__(self, model_path=None):
        super().__init__(model_path)
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise RuntimeError(
                "onnxruntime is not installed -- pip install onnxruntime, or run the "
                "CPU Docker image (see docker/README.md)") from exc
        self._anchors = postprocess.load_anchors()
        so = _session_options(ort)
        available = ort.get_available_providers()
        fp16_input = True
        if "CoreMLExecutionProvider" in available:
            # Core ML's format depends on the input dtype; a CPU session reads it.
            probe = ort.InferenceSession(self.model_path, so, providers=["CPUExecutionProvider"])
            fp16_input = "float16" in probe.get_inputs()[0].type
            del probe
        providers = _onnx_providers(available, fp16_input)
        try:
            self._session = ort.InferenceSession(self.model_path, so, providers=providers)
        except Exception:
            if providers == ["CPUExecutionProvider"]:
                raise
            # Not every graph compiles for Core ML; CPU always works.
            log.exception("Core ML session failed for %s; falling back to CPU", self.model_path)
            self._session = ort.InferenceSession(self.model_path, so,
                                                 providers=["CPUExecutionProvider"])
        self.provider = self._session.get_providers()[0]
        inp = self._session.get_inputs()[0]
        self._input_name = inp.name
        # NCHW; a dynamic dim comes back as a string, so fall back to 640.
        side = inp.shape[2] if len(inp.shape) == 4 else None
        self.input_size = side if isinstance(side, int) else 640
        # Ultralytics release ONNX is fp16; honour the graph's input dtype.
        self._in_dtype = np.float16 if "float16" in inp.type else np.float32
        self._mode = postprocess.layout([o.shape for o in self._session.get_outputs()],
                                        len(self._labels))
        log.info("CPU detector initialized (%s, mode=%s, provider=%s, input=%d)",
                 self.model_path, self._mode, self.provider, self.input_size)

    def _infer(self, frame_rgb):
        # NHWC uint8 -> NCHW float [1,3,H,W], scaled to [0,1].
        chw = frame_rgb.transpose(2, 0, 1).astype(np.float32) / 255.0
        batched = chw[np.newaxis, ...].astype(self._in_dtype)
        outputs = self._session.run(None, {self._input_name: batched})
        outputs = [np.asarray(o, dtype=np.float32) for o in outputs]
        if self._mode == "ultralytics":
            return postprocess.post_process_ultralytics(outputs[0])
        if self._mode == "anchor_free":
            return postprocess.post_process_anchor_free(outputs[0])
        size = (self.input_size, self.input_size)
        return postprocess.post_process_raw_heads(outputs, self._anchors, size)
