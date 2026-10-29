# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Detection post-processing shared by every detector backend.

Four output layouts are handled:

  * raw heads -- three ``[1,255,H,W]`` tensors needing the anchor/grid decode
    (Rockchip rknn_model_zoo yolov5s_relu; adapted from that repo's reference
    script).
  * Ultralytics -- one ``[1,N,5+num_classes]`` tensor with box decode and
    sigmoid baked in (ONNX / TFLite / Edge TPU exports).
  * anchor-free -- one ``[1,4+num_classes,N]`` tensor, no objectness column
    (YOLOv8/v9/v11, e.g. Frigate's ``yolov9-s-320.onnx``).
  * SSD post-processed -- TFLite_Detection_PostProcess's boxes/classes/scores/
    count, decode and NMS done in-graph (Google's Coral MobileDet / SSD
    MobileNet models, as Frigate ships).

The YOLO layouts feed the same per-class NMS; all three end in the COCO ->
person/vehicle/animal mapping.
Pure numpy, so it runs (and is unit-tested) off-device.
"""
import os

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(os.path.dirname(HERE), "models")
LABELS_PATH = os.path.join(MODELS_DIR, "coco_80_labels_list.txt")
# 90-id COCO list ("n/a" for unused ids) indexed by the SSD models' class ids.
LABELS_90_PATH = os.path.join(MODELS_DIR, "coco_90_labels.txt")
ANCHORS_PATH = os.path.join(MODELS_DIR, "anchors_yolov5.txt")

# Objectness x class gate; raised from 0.35 to suppress night false "person" hits
# (0.36-0.48). Override with AIPORT_OBJ_THRESH.
OBJ_THRESH = float(os.environ.get("AIPORT_OBJ_THRESH", "0.5"))
NMS_THRESH = 0.45

# AI Port's objectType vocabulary is just person/vehicle/animal; map YOLO's
# 80 COCO classes down onto those and drop the rest.
_VEHICLE_CLASSES = {"bicycle", "car", "motorbike", "aeroplane", "bus", "train", "truck", "boat"}
_ANIMAL_CLASSES = {"bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"}


def map_object_type(coco_label):
    label = coco_label.strip()
    if label == "person":
        return "person"
    if label in _VEHICLE_CLASSES:
        return "vehicle"
    if label in _ANIMAL_CLASSES:
        return "animal"
    return None


def load_anchors():
    with open(ANCHORS_PATH) as f:
        values = [float(x.strip()) for x in f if x.strip()]
    # 9 (x,y) pairs in 3 head-scales of 3 anchors, matching the model's
    # 80x80/40x40/20x20 output heads.
    pairs = list(zip(values[0::2], values[1::2]))
    return [pairs[0:3], pairs[3:6], pairs[6:9]]


def load_labels(path=LABELS_PATH):
    with open(path) as f:
        return [line.strip() for line in f if line.strip()]


def _box_process(position, anchors, img_size):
    grid_h, grid_w = position.shape[2:4]
    col, row = np.meshgrid(np.arange(0, grid_w), np.arange(0, grid_h))
    col = col.reshape(1, 1, grid_h, grid_w)
    row = row.reshape(1, 1, grid_h, grid_w)
    grid = np.concatenate((col, row), axis=1)
    stride = np.array([img_size[1] // grid_h, img_size[0] // grid_w]).reshape(1, 2, 1, 1)

    col = col.repeat(len(anchors), axis=0)
    row = row.repeat(len(anchors), axis=0)
    anchors = np.array(anchors).reshape(len(anchors), 2, 1, 1)

    box_xy = position[:, :2, :, :] * 2 - 0.5
    box_wh = pow(position[:, 2:4, :, :] * 2, 2) * anchors

    box_xy += grid
    box_xy *= stride
    box = np.concatenate((box_xy, box_wh), axis=1)

    xyxy = np.copy(box)
    xyxy[:, 0, :, :] = box[:, 0, :, :] - box[:, 2, :, :] / 2
    xyxy[:, 1, :, :] = box[:, 1, :, :] - box[:, 3, :, :] / 2
    xyxy[:, 2, :, :] = box[:, 0, :, :] + box[:, 2, :, :] / 2
    xyxy[:, 3, :, :] = box[:, 1, :, :] + box[:, 3, :, :] / 2
    return xyxy


def _filter_boxes(boxes, box_confidences, box_class_probs):
    box_confidences = box_confidences.reshape(-1)
    class_max_score = np.max(box_class_probs, axis=-1)
    classes = np.argmax(box_class_probs, axis=-1)
    pos = np.where(class_max_score * box_confidences >= OBJ_THRESH)
    scores = (class_max_score * box_confidences)[pos]
    return boxes[pos], classes[pos], scores


def nms_boxes(boxes, scores):
    x = boxes[:, 0]
    y = boxes[:, 1]
    w = boxes[:, 2] - boxes[:, 0]
    h = boxes[:, 3] - boxes[:, 1]
    areas = w * h
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x[i], x[order[1:]])
        yy1 = np.maximum(y[i], y[order[1:]])
        xx2 = np.minimum(x[i] + w[i], x[order[1:]] + w[order[1:]])
        yy2 = np.minimum(y[i] + h[i], y[order[1:]] + h[order[1:]])
        w1 = np.maximum(0.0, xx2 - xx1 + 0.00001)
        h1 = np.maximum(0.0, yy2 - yy1 + 0.00001)
        inter = w1 * h1
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        inds = np.where(ovr <= NMS_THRESH)[0]
        order = order[inds + 1]
    return np.array(keep)


def _nms_per_class(boxes, classes, scores):
    nboxes, nclasses, nscores = [], [], []
    for c in set(classes.tolist()):
        idx = np.where(classes == c)
        b, s = boxes[idx], scores[idx]
        keep = nms_boxes(b, s)
        if len(keep):
            nboxes.append(b[keep])
            nclasses.append(np.full(len(keep), c))
            nscores.append(s[keep])
    if not nboxes:
        return None, None, None
    return np.concatenate(nboxes), np.concatenate(nclasses), np.concatenate(nscores)


def post_process_raw_heads(outputs, anchors, img_size):
    """Three raw [1,255,H,W] heads -> (boxes xyxy px, classes, scores), or Nones."""
    boxes, scores, classes_conf = [], [], []
    reshaped = [o.reshape([len(anchors[0]), -1] + list(o.shape[-2:])) for o in outputs]
    for i, data in enumerate(reshaped):
        boxes.append(_box_process(data[:, :4, :, :], anchors[i], img_size))
        scores.append(data[:, 4:5, :, :])
        classes_conf.append(data[:, 5:, :, :])

    def flatten(x):
        ch = x.shape[1]
        return x.transpose(0, 2, 3, 1).reshape(-1, ch)

    boxes = np.concatenate([flatten(b) for b in boxes])
    classes_conf = np.concatenate([flatten(c) for c in classes_conf])
    scores = np.concatenate([flatten(s) for s in scores])

    boxes, classes, scores = _filter_boxes(boxes, scores, classes_conf)
    if boxes.shape[0] == 0:
        return None, None, None
    return _nms_per_class(boxes, classes, scores)


def post_process_ultralytics(output, box_scale=1.0):
    """One [1,N,5+classes] output -> (boxes xyxy px, classes, scores), or Nones.
    box_scale multiplies the xywh columns: 1 for pixel-space exports (ONNX), the
    input size for TFLite/Edge TPU exports, which emit 0-1 normalized boxes."""
    pred = output[0]  # (N, 5+num_classes): xywh, obj, class scores
    obj = pred[:, 4]
    class_conf = pred[:, 5:]
    classes = np.argmax(class_conf, axis=1)
    scores = obj * class_conf[np.arange(class_conf.shape[0]), classes]
    keep = scores >= OBJ_THRESH
    if not np.any(keep):
        return None, None, None
    xywh = pred[keep, :4] * box_scale
    xyxy = np.copy(xywh)
    xyxy[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
    xyxy[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
    xyxy[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
    xyxy[:, 3] = xywh[:, 1] + xywh[:, 3] / 2
    return _nms_per_class(xyxy, classes[keep], scores[keep])


def post_process_anchor_free(output, box_scale=1.0):
    """One [1,4+classes,N] output (YOLOv8/v9/v11) -> (boxes xyxy px, classes, scores),
    or Nones. No objectness: a box's score is its best class score."""
    pred = output[0].T  # (N, 4+num_classes): xywh, class scores
    class_conf = pred[:, 4:]
    classes = np.argmax(class_conf, axis=1)
    scores = class_conf[np.arange(class_conf.shape[0]), classes]
    keep = scores >= OBJ_THRESH
    if not np.any(keep):
        return None, None, None
    xywh = pred[keep, :4] * box_scale
    xyxy = np.copy(xywh)
    xyxy[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
    xyxy[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
    xyxy[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
    xyxy[:, 3] = xywh[:, 1] + xywh[:, 3] / 2
    return _nms_per_class(xyxy, classes[keep], scores[keep])


def layout(output_shapes, num_classes):
    """'ultralytics' ([1,N,5+nc]), 'anchor_free' ([1,4+nc,N]) or 'raw_heads'."""
    if len(output_shapes) == 1 and len(output_shapes[0]) == 3:
        shape = output_shapes[0]
        if shape[-1] == 5 + num_classes:
            return "ultralytics"
        if shape[1] == 4 + num_classes:
            return "anchor_free"
    return "raw_heads"


def resize(frame_rgb, size):
    """Resize an HxWx3 frame to size x size: OpenCV's area filter when present, else
    nearest-neighbour in numpy (no hard OpenCV dependency)."""
    try:
        import cv2
        return cv2.resize(frame_rgb, (size, size), interpolation=cv2.INTER_AREA)
    except ImportError:
        ys = (np.arange(size) * frame_rgb.shape[0] / size).astype(int)
        xs = (np.arange(size) * frame_rgb.shape[1] / size).astype(int)
        return frame_rgb[ys][:, xs]


def post_process_ssd(boxes, classes, scores, count, img_size):
    """TFLite_Detection_PostProcess outputs -> (boxes xyxy px, classes, scores),
    or Nones. boxes are [1,N,4] normalized (ymin, xmin, ymax, xmax); NMS has
    already run in-graph."""
    n = min(int(np.asarray(count).reshape(-1)[0]), boxes.shape[1])
    b, c, s = boxes[0, :n], classes[0, :n].astype(np.int64), scores[0, :n]
    keep = s >= OBJ_THRESH
    if not np.any(keep):
        return None, None, None
    b = b[keep]
    xyxy = np.stack([b[:, 1], b[:, 0], b[:, 3], b[:, 2]], axis=1) * img_size
    return xyxy, c[keep], s[keep]


def to_results(boxes, classes, scores, labels):
    """NMS output -> [{"objectType", "label", "score", "box": (x1,y1,x2,y2)}], mapped
    to AI Port's person/vehicle/animal vocabulary (label: the COCO class), score-descending."""
    if boxes is None:
        return []
    results = []
    for box, cls, score in zip(boxes, classes, scores):
        coco_label = labels[int(cls)] if int(cls) < len(labels) else None
        mapped = map_object_type(coco_label) if coco_label else None
        if not mapped:
            continue
        x1, y1, x2, y2 = [float(v) for v in box]
        results.append({"objectType": mapped, "label": coco_label.strip(), "score": float(score),
                        "box": (x1, y1, x2, y2)})
    results.sort(key=lambda r: -r["score"])
    return results
