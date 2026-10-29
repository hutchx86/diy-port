# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Interface every detector backend implements."""
import threading

from . import postprocess


class Detector:
    """One shared model behind a lock -- accelerators serialize inference
    anyway, so it's as fast as one instance per camera.

    Subclasses set NAME and DEFAULT_MODEL, implement available() and
    _infer(), and set self.input_size in __init__.
    """

    NAME = ""
    DEFAULT_MODEL = ""

    def __init__(self, model_path=None):
        self.model_path = model_path or self.DEFAULT_MODEL
        self.input_size = 640  # square side in px; ffmpeg scales frames to this
        self._lock = threading.Lock()
        self._labels = postprocess.load_labels()

    @classmethod
    def available(cls, model_path=None):
        """(ok, reason): whether this backend's runtime, hardware and default
        model (or model_path) are present. Cheap -- loads nothing."""
        raise NotImplementedError

    def _infer(self, frame_rgb):
        """Run the model; return (boxes, classes, scores) in input_size pixel
        space, or Nones when nothing passes the threshold."""
        raise NotImplementedError

    def detect(self, frame_rgb, min_score=None):
        """frame_rgb: HxWx3 uint8 RGB of any size -- a whole frame or a region crop;
        it's stretched to input_size (no letterboxing) when it isn't already. Returns
        {"objectType", "label", "score", "box": (x1,y1,x2,y2)} in frame_rgb's pixel
        space, score-descending, mapped to AI Port's person/vehicle/animal vocabulary.
        min_score (optional) drops boxes below it, on top of the backend's threshold."""
        h, w = frame_rgb.shape[:2]
        if (w, h) != (self.input_size, self.input_size):
            frame_rgb = postprocess.resize(frame_rgb, self.input_size)
        with self._lock:
            boxes, classes, scores = self._infer(frame_rgb)
        results = postprocess.to_results(boxes, classes, scores, self._labels)
        sx, sy = w / self.input_size, h / self.input_size
        if (sx, sy) != (1.0, 1.0):
            for r in results:
                x1, y1, x2, y2 = r["box"]
                r["box"] = (x1 * sx, y1 * sy, x2 * sx, y2 * sy)
        if min_score is not None:
            results = [r for r in results if r["score"] >= min_score]
        return results
