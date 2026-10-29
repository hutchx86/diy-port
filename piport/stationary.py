# SPDX-License-Identifier: MIT AND AGPL-3.0-or-later
#
# Adapted from Frigate's frigate/track/stationary_classifier.py (StationaryMotionClassifier),
# https://github.com/blakeblackshear/frigate
#
# The MIT License
# Copyright (c) 2026 Frigate, Inc. (Frigate™)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this
# software and associated documentation files (the "Software"), to deal in the Software
# without restriction, including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
# to whom the Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or
# substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
# FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.
#
# Changes for pi-port: per-track state in a plain dict instead of the classifier's id maps;
# a grayscale frame instead of a YUV one; cv2.GaussianBlur instead of scipy.
"""Did a parked object really move? Compare its spot's pixels, not its boxes.

When a track parks, a crop of the frame inside its box becomes the anchor. Later, when its
detection boxes suggest it moved, the same spot is cropped again and compared with the
anchor: normalised cross-correlation for "does it still look the same", phase correlation
for "has the picture there shifted". Boxes on a parked car jitter (people walking past,
weak or partial detections); the car's pixels don't.
"""
import cv2
import numpy as np

CROP_SIZE = 96
NCC_KEEP_THRESHOLD = 0.90     # high correlation: still there
NCC_ACTIVE_THRESHOLD = 0.85   # low correlation: changed
SHIFT_KEEP_THRESHOLD = 0.02   # small shift: still there
SHIFT_ACTIVE_THRESHOLD = 0.04  # large shift: moved
DRIFT_ACTIVE_THRESHOLD = 0.12  # summed shift over the last 5 checks
CHANGED_FRAMES_TO_FLIP = 2

_HANN = np.outer(np.hanning(CROP_SIZE), np.hanning(CROP_SIZE)).astype(np.float64)


def _crop(gray, box):
    """Blurred CROP_SIZE x CROP_SIZE crop of gray inside box (x1, y1, x2, y2), clamped."""
    h, w = gray.shape[:2]
    x1 = max(0, min(w - 1, int(box[0])))
    y1 = max(0, min(h - 1, int(box[1])))
    x2 = max(0, min(w - 1, int(box[2])))
    y2 = max(0, min(h - 1, int(box[3])))
    if x2 <= x1:
        x2 = min(w - 1, x1 + 1)
    if y2 <= y1:
        y2 = min(h - 1, y1 + 1)
    crop = cv2.resize(gray[y1:y2, x1:x2], (CROP_SIZE, CROP_SIZE), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(crop, (3, 3), 0.5)


def anchor(gray, box):
    """State for a newly parked object at box (frame pixels)."""
    return {"box": tuple(box), "crop": _crop(gray, box), "changed": 0, "shifts": []}


def still_there(state, gray):
    """True to keep the object parked, False once its spot has really changed (on
    CHANGED_FRAMES_TO_FLIP checks in a row, or a large drift). Updates state."""
    current = _crop(gray, state["box"])
    ncc = float(cv2.matchTemplate(current, state["crop"], cv2.TM_CCOEFF_NORMED)[0, 0])
    (sx, sy), _ = cv2.phaseCorrelate(state["crop"].astype(np.float64) * _HANN,
                                     current.astype(np.float64) * _HANN)
    shift = float(np.hypot(sx, sy)) / CROP_SIZE
    state["shifts"] = (state["shifts"] + [shift])[-5:]
    drift = sum(state["shifts"])
    state["last"] = (ncc, shift)
    if ncc >= NCC_KEEP_THRESHOLD and shift < SHIFT_KEEP_THRESHOLD:
        state["changed"] = 0
        return True
    if ncc < NCC_ACTIVE_THRESHOLD or shift >= SHIFT_ACTIVE_THRESHOLD or drift >= DRIFT_ACTIVE_THRESHOLD:
        state["changed"] += 1
        return not (state["changed"] >= CHANGED_FRAMES_TO_FLIP or drift >= DRIFT_ACTIVE_THRESHOLD)
    state["changed"] = 0
    return True
