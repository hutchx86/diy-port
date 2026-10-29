# SPDX-License-Identifier: MIT AND AGPL-3.0-or-later
#
# Adapted from Frigate's frigate/track/norfair_tracker.py (distance, per-type tracker
# settings), https://github.com/blakeblackshear/frigate
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
# Changes for pi-port: Norfair only associates detections with objects; pi-port's tracker
# keeps confirmation, parking, events and miss tolerance. No PTZ or re-ID settings.
"""Which detection belongs to which object: Norfair's Kalman-filtered tracker (BSD-3-Clause)
with Frigate's distance and settings, one tracker per object type.

The distance compares the bottom-centre of a detection with the object's predicted box,
relative to its size, plus the change in width and height; Norfair's Kalman filter predicts
where each object will be from how it has been moving, so a fast walker or a car crossing
the frame keeps its object between frames.
"""
import logging
from collections import defaultdict

import numpy as np
from norfair import Detection, Tracker
from norfair.filter import OptimizedKalmanFilterFactory


class _QuietScalarWarning(logging.Filter):
    """Norfair warns on every tracker that a scalar distance is slower; Frigate uses one too."""

    def filter(self, record):
        return "scalar distance function" not in record.getMessage()


logging.getLogger().addFilter(_QuietScalarWarning())

# Per pi-port objectType, after Frigate's per-label settings (car, license_plate, default).
TYPE_SETTINGS = {
    "vehicle": {"R": 3.4, "Q": 0.03, "distance_threshold": 2.5},
    "licensePlate": {"R": 2.5, "Q": 0.05, "distance_threshold": 3.75},
}
DEFAULT_SETTINGS = {"R": 3.4, "distance_threshold": 2.5}


def distance(detection, estimate):
    """Frigate's distance between two boxes given as [[x1, y1], [x2, y2]]: bottom-centre
    offset relative to the estimate's size, plus the width and height ratios."""
    estimate_dim = np.diff(estimate, axis=0).flatten()
    detection_dim = np.diff(detection, axis=0).flatten()
    if (not np.all(np.isfinite(estimate_dim)) or not np.all(np.isfinite(detection_dim))
            or estimate_dim[0] <= 0 or estimate_dim[1] <= 0
            or detection_dim[0] <= 0 or detection_dim[1] <= 0):
        return float("inf")
    detection_pos = np.array([np.average(detection[:, 0]), np.max(detection[:, 1])])
    estimate_pos = np.array([np.average(estimate[:, 0]), np.max(estimate[:, 1])])
    offset = (detection_pos - estimate_pos).astype(float)
    offset[0] /= estimate_dim[0]
    offset[1] /= estimate_dim[1]
    widths = np.sort([estimate_dim[0], detection_dim[0]])
    heights = np.sort([estimate_dim[1], detection_dim[1]])
    change = np.append(offset, [widths[1] / widths[0] - 1.0, heights[1] / heights[0] - 1.0])
    return float(np.linalg.norm(change))


def _frigate_distance(detection, tracked_object):
    return distance(detection.points, tracked_object.estimate)


class Matcher:
    """One Norfair tracker per object type. hit_counter_max: frames an object lives on
    without a detection (pi-port closes its own tracks sooner or later on top of this)."""

    def __init__(self, hit_counter_max):
        self.hit_counter_max = max(1, int(hit_counter_max))
        self.trackers = {}

    def _tracker(self, object_type):
        if object_type not in self.trackers:
            s = TYPE_SETTINGS.get(object_type, DEFAULT_SETTINGS)
            factory = (OptimizedKalmanFilterFactory(R=s["R"], Q=s["Q"]) if "Q" in s
                       else OptimizedKalmanFilterFactory(R=s["R"]))
            self.trackers[object_type] = Tracker(
                distance_function=_frigate_distance,
                distance_threshold=s["distance_threshold"],
                initialization_delay=0,  # pi-port confirms tracks itself (MIN_HITS, median)
                hit_counter_max=self.hit_counter_max,
                filter_factory=factory)
        return self.trackers[object_type]

    def match(self, detections):
        """{detection index: Norfair global id} for every detection associated with an
        object this frame, new objects included. detections: dicts with "objectType" and
        "coord" [x, y, w, h]."""
        by_type = defaultdict(list)
        for i, d in enumerate(detections):
            x, y, w, h = d["coord"]
            by_type[d["objectType"]].append(
                Detection(points=np.array([[x, y], [x + w, y + h]], dtype=float), data={"idx": i}))
        out = {}
        for object_type in set(by_type) | set(self.trackers):
            dets = by_type.get(object_type, [])
            for obj in self._tracker(object_type).update(detections=dets):
                if any(obj.last_detection is d for d in dets):  # detected this frame
                    out[obj.last_detection.data["idx"]] = obj.global_id
        return out
