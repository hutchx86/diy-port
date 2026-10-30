# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Pluggable object-detection backends.

Every backend takes a square RGB frame at its own ``input_size`` and returns
person/vehicle/animal boxes (see base.Detector). Pick one with [hardware]
detector in aiport.cfg (or AIPORT_DETECTOR); "auto" takes the first backend
in AUTO_ORDER whose runtime, hardware and model are all present.

Adding a backend: subclass base.Detector in a new module and list it in
BACKENDS (and AUTO_ORDER if it should be auto-picked).
"""
import logging
import os

from .cpu import CpuDetector
from .edgetpu import EdgeTpuDetector
from .rknn import RknnDetector

log = logging.getLogger("aiport-detector")

BACKENDS = {cls.NAME: cls for cls in (RknnDetector, EdgeTpuDetector, CpuDetector)}
# Fastest first; cpu is the fallback that works everywhere.
AUTO_ORDER = ("rknn", "edgetpu", "cpu")


def probe(model_path=None):
    """{name: (ok, reason)} for every backend -- for logs and the installer."""
    return {name: cls.available(model_path) for name, cls in BACKENDS.items()}


def resolve(name="auto", model_path=None):
    """Backend class for `name`; "auto" picks the first available one."""
    name = (name or "auto").strip().lower()
    if name == "onnx":  # legacy AIPORT_BACKEND name for the CPU backend
        name = "cpu"
    if name == "auto" and model_path:
        # An explicit model decides the backend, as the pre-modular detector did.
        ext = os.path.splitext(model_path)[1].lower()
        name = {".rknn": "rknn", ".tflite": "edgetpu", ".onnx": "cpu"}.get(ext, "auto")
    if name != "auto":
        if name not in BACKENDS:
            raise ValueError(f"unknown detector {name!r}; choose from auto, {', '.join(BACKENDS)}")
        return BACKENDS[name]
    reasons = []
    for candidate in AUTO_ORDER:
        ok, why = BACKENDS[candidate].available(model_path)
        if ok:
            return BACKENDS[candidate]
        reasons.append(f"{candidate}: {why}")
    raise RuntimeError("no detector backend available (" + "; ".join(reasons) + ")")


def create(name="auto", model_path=None):
    """Instantiate the chosen backend (loads the model onto its hardware)."""
    cls = resolve(name, model_path)
    log.info("using detector backend %r", cls.NAME)
    return cls(model_path or None)
