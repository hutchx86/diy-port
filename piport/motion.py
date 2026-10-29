# SPDX-License-Identifier: MIT AND AGPL-3.0-or-later
#
# Adapted from Frigate's frigate/motion/improved_motion.py (ImprovedMotionDetector),
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
# Changes for pi-port: no PTZ handling or debug images; cv2.GaussianBlur instead of scipy;
# the mask is built from Protect's motion zones; settings are plain arguments.
"""Motion boxes from a background model (Frigate's "improved" motion detector).

Each frame is shrunk to `frame_height` rows of grayscale, contrast-stretched, blurred and
compared with a running-average background; pixels that differ by more than `threshold`
form contours, and contours larger than `contour_area` become motion boxes. A frame where
more than `lightning_threshold` of the picture changes (lightning, IR switching) puts the
detector back into calibration rather than reporting motion.
"""
import cv2
import numpy as np

DEFAULT_THRESHOLD = 30
DEFAULT_CONTOUR_AREA = 10
DEFAULT_FRAME_ALPHA = 0.01
DEFAULT_LIGHTNING_THRESHOLD = 0.8
DEFAULT_FRAME_HEIGHT = 100
CALIBRATING_ALPHA = 0.2
CONTRAST_HISTORY = 50


class MotionDetector:
    def __init__(self, frame_shape, threshold=DEFAULT_THRESHOLD,
                 contour_area=DEFAULT_CONTOUR_AREA, frame_alpha=DEFAULT_FRAME_ALPHA,
                 lightning_threshold=DEFAULT_LIGHTNING_THRESHOLD,
                 frame_height=DEFAULT_FRAME_HEIGHT, improve_contrast=True, mask=None):
        """frame_shape: (height, width) of the frames passed to detect(). mask: optional
        uint8 array of frame_shape, 0 where motion is ignored."""
        self.frame_shape = tuple(frame_shape[:2])
        self.threshold = threshold
        self.contour_area = contour_area
        self.frame_alpha = frame_alpha
        self.lightning_threshold = lightning_threshold
        self.improve_contrast = improve_contrast
        self.resize_factor = self.frame_shape[0] / frame_height
        self.motion_frame_size = (frame_height,
                                  frame_height * self.frame_shape[1] // self.frame_shape[0])
        self.contrast_values = np.zeros((CONTRAST_HISTORY, 2), np.uint8)
        self.contrast_values[:, 1:2] = 255
        self.contrast_values_index = 0
        self.set_mask(mask)

    def set_mask(self, mask):
        """Replace the mask (None = none) and recalibrate, as Frigate does on a mask change."""
        if mask is None:
            self.mask = None
        else:
            small = cv2.resize(mask, dsize=(self.motion_frame_size[1], self.motion_frame_size[0]),
                               interpolation=cv2.INTER_AREA)
            self.mask = np.where(small == 0)
        self.avg_frame = np.zeros(self.motion_frame_size, np.float32)
        self.calibrating = True
        self.motion_frame_count = 0

    def is_calibrating(self):
        return self.calibrating

    def detect(self, gray):
        """Motion boxes (x1, y1, x2, y2) in frame pixels for a grayscale frame."""
        motion_boxes = []
        resized = cv2.resize(gray, dsize=(self.motion_frame_size[1], self.motion_frame_size[0]),
                             interpolation=cv2.INTER_NEAREST)

        if self.improve_contrast:
            min_value = np.percentile(resized, 4).astype(np.uint8)
            max_value = np.percentile(resized, 96).astype(np.uint8)
            if min_value < max_value:  # skip a single-colour image
                self.contrast_values[self.contrast_values_index] = [min_value, max_value]
                self.contrast_values_index = (self.contrast_values_index + 1) % CONTRAST_HISTORY
                avg_min, avg_max = np.mean(self.contrast_values, axis=0)
                resized = np.clip(resized, avg_min, avg_max)
                resized = (((resized - avg_min) / (avg_max - avg_min)) * 255).astype(np.uint8)

        if self.mask is not None:
            resized[self.mask] = 0  # after the contrast step, to match the startup average
        resized = cv2.GaussianBlur(resized, (3, 3), 1)

        frame_delta = cv2.absdiff(resized, cv2.convertScaleAbs(self.avg_frame))
        thresh = cv2.threshold(frame_delta, self.threshold, 255, cv2.THRESH_BINARY)[1]
        thresh = cv2.dilate(thresh, None, iterations=1)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        total_area = 0.0
        for c in contours:
            area = cv2.contourArea(c)
            total_area += area
            if area > self.contour_area:
                x, y, w, h = cv2.boundingRect(c)
                f = self.resize_factor
                motion_boxes.append((int(x * f), int(y * f), int((x + w) * f), int((y + h) * f)))
        pct_motion = total_area / (self.motion_frame_size[0] * self.motion_frame_size[1])

        # Calibrated once motion is under 5% of the picture in at most 4 places; lightning,
        # IR switching and the like (more than lightning_threshold) recalibrate.
        if pct_motion < 0.05 and len(motion_boxes) <= 4:
            self.calibrating = False
        if self.calibrating or pct_motion > self.lightning_threshold:
            self.calibrating = True

        alpha = CALIBRATING_ALPHA if self.calibrating else self.frame_alpha
        if motion_boxes:
            # Only blend in a frame with motion once the difference has persisted a while.
            self.motion_frame_count += 1
            if self.motion_frame_count >= 10:
                cv2.accumulateWeighted(resized, self.avg_frame, alpha)
        else:
            cv2.accumulateWeighted(resized, self.avg_frame, alpha)
            self.motion_frame_count = 0
        return motion_boxes


def threshold_for_level(level):
    """Protect's motion sensitivity (0-100, higher = more sensitive; 50 by default) as a
    pixel threshold: 50 -> Frigate's 30, linearly, kept within 5-60."""
    try:
        level = float(level)
    except (TypeError, ValueError):
        return DEFAULT_THRESHOLD
    return int(round(min(60, max(5, 60 - 0.6 * level))))


def zones_mask(zones, frame_shape, to_image):
    """uint8 mask (255 = watched) from Protect's motion `zones` ({"1": {"coord": [x0, y0,
    x1, y1, ...]}}, 0-1000 on Protect's canvas), or None when there are none or one covers
    the whole picture. to_image maps a canvas point (x, y) to image-normalised 0-1000."""
    polys = []
    for zone in (zones or {}).values():
        coord = (zone or {}).get("coord") or []
        pts = [to_image(coord[i], coord[i + 1]) for i in range(0, len(coord) - 1, 2)]
        if len(pts) >= 3:
            polys.append(pts)
    if not polys:
        return None
    h, w = frame_shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for pts in polys:
        px = np.array([[x / 1000 * w, y / 1000 * h] for x, y in pts], np.int32)
        cv2.fillPoly(mask, [px], 255)
    if mask.min() == 255:
        return None
    return mask
