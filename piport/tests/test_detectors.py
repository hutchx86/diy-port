# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for the detectors package: the shared YOLO/SSD post-processing,
backend selection, and the Edge TPU input/output helpers. Skipped if numpy
isn't importable; no accelerator runtime is needed.
"""
import os
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import numpy as np
    import detectors
    from detectors import postprocess
    from detectors import edgetpu
    from detectors import cpu
    from detectors.edgetpu import EdgeTpuDetector
    _IMPORT_ERR = None
except Exception as exc:  # numpy missing, etc.
    detectors = None
    _IMPORT_ERR = exc


@unittest.skipIf(detectors is None, f"detectors unavailable ({_IMPORT_ERR})")
class TestMapObjectType(unittest.TestCase):
    def test_mapping(self):
        self.assertEqual(postprocess.map_object_type("person"), "person")
        for v in ("car", "bus", "truck", "bicycle", "motorbike", "train", "boat", "aeroplane"):
            self.assertEqual(postprocess.map_object_type(v), "vehicle", v)
        for a in ("bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe"):
            self.assertEqual(postprocess.map_object_type(a), "animal", a)

    def test_unmapped(self):
        for u in ("toaster", "banana", "traffic light", ""):
            self.assertIsNone(postprocess.map_object_type(u))

    def test_strips_whitespace(self):
        self.assertEqual(postprocess.map_object_type("  person\n"), "person")


@unittest.skipIf(detectors is None, f"detectors unavailable ({_IMPORT_ERR})")
class TestRawHeads(unittest.TestCase):
    def setUp(self):
        self.anchors = postprocess.load_anchors()
        self.labels = postprocess.load_labels()

    def _zeros(self, size):
        # 3 heads at strides 8/16/32; 3 anchors per head; 85 channels each
        # (4 box + 1 objectness + 80 classes)
        return [np.zeros((1, 255, size // s, size // s), dtype=np.float32) for s in (8, 16, 32)]

    def test_all_zero_yields_nothing(self):
        boxes, classes, scores = postprocess.post_process_raw_heads(
            self._zeros(640), self.anchors, (640, 640))
        self.assertIsNone(boxes)
        self.assertIsNone(classes)
        self.assertIsNone(scores)

    def _one_person(self, size):
        outs = self._zeros(size)
        # strong person (COCO class 0) at the centre cell of the stride-8 head,
        # anchor 0. Channels: [0:4]=box, [4]=objectness, [5+cls]=class prob.
        g, c = outs[0], size // 16
        g[0, 0:4, c, c] = 0.5
        g[0, 4, c, c] = 1.0
        g[0, 5 + 0, c, c] = 1.0
        return postprocess.post_process_raw_heads(outs, self.anchors, (size, size))

    def test_one_person_detection(self):
        boxes, classes, scores = self._one_person(640)
        self.assertIsNotNone(boxes)
        self.assertIn(0, list(classes))
        self.assertGreater(float(max(scores)), 0.9)
        self.assertEqual(self.labels[0], "person")
        x1, y1, x2, y2 = boxes[0]
        self.assertLessEqual(x1, x2)
        self.assertLessEqual(y1, y2)
        # centre cell (40,40) at stride 8 decodes to ~(324,324)
        self.assertAlmostEqual((x1 + x2) / 2, 324, delta=1)

    def test_input_size_sets_stride(self):
        boxes, _, _ = self._one_person(320)
        x1, _, x2, _ = boxes[0]
        self.assertAlmostEqual((x1 + x2) / 2, 164, delta=1)  # cell 20 at stride 8


@unittest.skipIf(detectors is None, f"detectors unavailable ({_IMPORT_ERR})")
class TestUltralytics(unittest.TestCase):
    def _pred(self, rows):
        out = np.zeros((1, len(rows), 85), dtype=np.float32)
        for i, (xywh, obj, cls) in enumerate(rows):
            out[0, i, :4] = xywh
            out[0, i, 4] = obj
            out[0, i, 5 + cls] = 1.0
        return out

    def test_pixel_boxes(self):
        out = self._pred([((100, 100, 20, 40), 0.9, 2)])
        boxes, classes, scores = postprocess.post_process_ultralytics(out)
        np.testing.assert_allclose(boxes[0], [90, 80, 110, 120])
        self.assertEqual(int(classes[0]), 2)

    def test_normalized_boxes_scaled(self):
        out = self._pred([((0.5, 0.5, 0.1, 0.2), 0.9, 0)])
        boxes, _, _ = postprocess.post_process_ultralytics(out, box_scale=320)
        np.testing.assert_allclose(boxes[0], [144, 128, 176, 192], atol=1e-3)

    def test_below_threshold(self):
        out = self._pred([((100, 100, 20, 40), 0.1, 0)])
        self.assertIsNone(postprocess.post_process_ultralytics(out)[0])

    def test_duplicates_suppressed(self):
        out = self._pred([((100, 100, 20, 20), 0.9, 0), ((101, 101, 20, 20), 0.8, 0)])
        boxes, _, _ = postprocess.post_process_ultralytics(out)
        self.assertEqual(len(boxes), 1)

    def test_to_results_maps_and_drops(self):
        labels = postprocess.load_labels()
        out = self._pred([((100, 100, 20, 20), 0.9, labels.index("car")),
                          ((300, 300, 20, 20), 0.95, labels.index("toaster"))])
        results = postprocess.to_results(*postprocess.post_process_ultralytics(out), labels)
        self.assertEqual([r["objectType"] for r in results], ["vehicle"])


@unittest.skipIf(detectors is None, f"detectors unavailable ({_IMPORT_ERR})")
class TestNMS(unittest.TestCase):
    """Regression guard for nms_boxes: a missing '+ w[others]'/'+ h[others]'
    term makes the intersection always zero, so NMS silently keeps every
    duplicate detection."""

    def test_nms(self):
        dup = np.array([[0, 0, 10, 10], [0, 0, 10, 10], [100, 100, 110, 110]], dtype=np.float32)
        self.assertEqual(len(postprocess.nms_boxes(dup, np.array([0.9, 0.8, 0.7], np.float32))), 2)
        heavy = np.array([[0, 0, 10, 10], [1, 1, 11, 11]], dtype=np.float32)
        self.assertEqual(len(postprocess.nms_boxes(heavy, np.array([0.9, 0.8], np.float32))), 1)
        light = np.array([[0, 0, 10, 10], [5, 5, 15, 15]], dtype=np.float32)
        self.assertEqual(len(postprocess.nms_boxes(light, np.array([0.9, 0.8], np.float32))), 2)


@unittest.skipIf(detectors is None, f"detectors unavailable ({_IMPORT_ERR})")
class TestResolve(unittest.TestCase):
    def _avail(self, **ok):
        return [mock.patch.object(detectors.BACKENDS[n], "available",
                                  return_value=(ok.get(n, False), "absent"))
                for n in detectors.BACKENDS]

    def _with(self, fn, **ok):
        patches = self._avail(**ok)
        for p in patches:
            p.start()
        try:
            return fn()
        finally:
            for p in patches:
                p.stop()

    def test_auto_prefers_npu_then_tpu_then_cpu(self):
        self.assertEqual(self._with(detectors.resolve, rknn=True, edgetpu=True, cpu=True).NAME, "rknn")
        self.assertEqual(self._with(detectors.resolve, edgetpu=True, cpu=True).NAME, "edgetpu")
        self.assertEqual(self._with(detectors.resolve, cpu=True).NAME, "cpu")

    def test_auto_none_available(self):
        with self.assertRaises(RuntimeError) as cm:
            self._with(detectors.resolve)
        self.assertIn("edgetpu: absent", str(cm.exception))

    def test_explicit_name_skips_probe(self):
        self.assertEqual(detectors.resolve(" EdgeTPU ").NAME, "edgetpu")

    def test_legacy_onnx_name_is_cpu(self):
        self.assertEqual(detectors.resolve("onnx").NAME, "cpu")

    def test_model_extension_selects_backend(self):
        self.assertEqual(detectors.resolve("auto", "/x/y.rknn").NAME, "rknn")
        self.assertEqual(detectors.resolve("auto", "/x/y.tflite").NAME, "edgetpu")
        self.assertEqual(detectors.resolve("auto", "/x/y.ONNX").NAME, "cpu")

    def test_unknown_name(self):
        with self.assertRaises(ValueError):
            detectors.resolve("tpu9000")


@unittest.skipIf(detectors is None, f"detectors unavailable ({_IMPORT_ERR})")
class TestOnnxProviders(unittest.TestCase):
    """AIPORT_ORT_PROVIDER picks Core ML when onnxruntime offers it, else CPU."""

    MAC = ["CoreMLExecutionProvider", "CPUExecutionProvider"]

    def setUp(self):
        saved = os.environ.pop("AIPORT_ORT_PROVIDER", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "AIPORT_ORT_PROVIDER", saved)
        self.addCleanup(os.environ.pop, "AIPORT_ORT_PROVIDER", None)

    def test_auto_prefers_coreml(self):
        p = cpu._onnx_providers(self.MAC)
        self.assertEqual(p[0][0], "CoreMLExecutionProvider")
        self.assertEqual(p[-1], "CPUExecutionProvider")

    def test_coreml_format_follows_input_dtype(self):
        fp16 = cpu._onnx_providers(self.MAC, fp16_input=True)[0][1]
        fp32 = cpu._onnx_providers(self.MAC, fp16_input=False)[0][1]
        self.assertEqual(fp16.get("ModelFormat"), "MLProgram")
        self.assertNotIn("ModelFormat", fp32)  # NeuralNetwork, Core ML's default

    def test_compute_units_setting(self):
        # Live 18:40 the Neural Engine, shared with Frigate's Core ML detector, was the
        # bottleneck; gpu moves pi-port's model off it, in the format that's fast there.
        self.addCleanup(os.environ.pop, "AIPORT_COREML_UNITS", None)
        os.environ["AIPORT_COREML_UNITS"] = "gpu"
        opts = cpu._onnx_providers(self.MAC, fp16_input=False)[0][1]
        self.assertEqual(opts, {"MLComputeUnits": "CPUAndGPU", "ModelFormat": "MLProgram"})
        os.environ["AIPORT_COREML_UNITS"] = "ane"
        self.assertEqual(cpu._onnx_providers(self.MAC, fp16_input=False)[0][1],
                         {"MLComputeUnits": "CPUAndNeuralEngine"})
        os.environ["AIPORT_COREML_UNITS"] = "auto"
        self.assertEqual(cpu._onnx_providers(self.MAC, fp16_input=False)[0][1], {})

    def test_auto_without_coreml(self):
        self.assertEqual(cpu._onnx_providers(["CPUExecutionProvider"]),
                         ["CPUExecutionProvider"])

    def test_forced_cpu(self):
        os.environ["AIPORT_ORT_PROVIDER"] = "cpu"
        self.assertEqual(cpu._onnx_providers(self.MAC), ["CPUExecutionProvider"])

    def test_coreml_compile_failure_falls_back_to_cpu(self):
        import types

        class _IO:
            def __init__(self, name, type_, shape):
                self.name, self.type, self.shape = name, type_, shape

        class _Session:
            def __init__(self, path, so, providers):
                if providers[0] != "CPUExecutionProvider":
                    raise RuntimeError("Unable to parse ML Program")
                self.providers = providers

            def get_providers(self):
                return self.providers

            def get_inputs(self):
                return [_IO("images", "tensor(float)", [1, 3, 640, 640])]

            def get_outputs(self):
                return [_IO("output", "tensor(float)", [1, 25200, 85])]

        class _Opts:
            def add_session_config_entry(self, k, v):
                pass

        fake = types.SimpleNamespace(InferenceSession=_Session, SessionOptions=_Opts,
                                     get_available_providers=lambda: self.MAC)
        with mock.patch.dict(sys.modules, {"onnxruntime": fake}), \
                self.assertLogs("aiport-detector", "ERROR"):
            backend = cpu.CpuDetector()
        self.assertEqual(backend.provider, "CPUExecutionProvider")


@unittest.skipIf(detectors is None, f"detectors unavailable ({_IMPORT_ERR})")
class TestSsd(unittest.TestCase):
    def _outs(self, rows, count=None):
        boxes = np.zeros((1, 10, 4), np.float32)
        classes = np.zeros((1, 10), np.float32)
        scores = np.zeros((1, 10), np.float32)
        for i, (yxyx, cls, score) in enumerate(rows):
            boxes[0, i], classes[0, i], scores[0, i] = yxyx, cls, score
        return boxes, classes, scores, np.array([len(rows) if count is None else count], np.float32)

    def test_decode_yxyx_to_pixels(self):
        out = self._outs([((0.25, 0.5, 0.75, 1.0), 0, 0.9)])
        boxes, classes, scores = postprocess.post_process_ssd(*out, 320)
        np.testing.assert_allclose(boxes[0], [160, 80, 320, 240])
        self.assertEqual(int(classes[0]), 0)

    def test_threshold_and_count(self):
        out = self._outs([((0, 0, 1, 1), 0, 0.9), ((0, 0, 1, 1), 2, 0.2),
                          ((0, 0, 1, 1), 2, 0.95)], count=2)
        _, classes, _ = postprocess.post_process_ssd(*out, 320)
        self.assertEqual(classes.tolist(), [0])  # 0.2 below threshold, third past count

    def test_nothing(self):
        self.assertIsNone(postprocess.post_process_ssd(*self._outs([]), 320)[0])

    def test_90_label_ids(self):
        labels = postprocess.load_labels(postprocess.LABELS_90_PATH)
        self.assertEqual(len(labels), 90)
        self.assertEqual(labels[0], "person")
        self.assertEqual(labels[2], "car")
        self.assertEqual(labels[17], "dog")  # past the "n/a" gap at id 11
        self.assertEqual(labels[11], "n/a")
        self.assertIsNone(postprocess.map_object_type(labels[11]))

    def test_output_order_by_name(self):
        def o(name, shape):
            return {"name": name, "shape": np.array(shape)}
        mobiledet = [o("TFLite_Detection_PostProcess", [1, 10, 4]),
                     o("TFLite_Detection_PostProcess:1", [1, 10]),
                     o("TFLite_Detection_PostProcess:2", [1, 10]),
                     o("TFLite_Detection_PostProcess:3", [1])]
        self.assertEqual(edgetpu._ssd_output_order(mobiledet), (0, 1, 2, 3))
        shuffled = [mobiledet[3], mobiledet[2], mobiledet[0], mobiledet[1]]
        self.assertEqual(edgetpu._ssd_output_order(shuffled), (2, 3, 1, 0))
        self.assertIsNone(edgetpu._ssd_output_order([o("Identity", [1, 6300, 85])]))


@unittest.skipIf(detectors is None, f"detectors unavailable ({_IMPORT_ERR})")
class TestEdgeTpuInput(unittest.TestCase):
    def _det(self, dtype):
        d = EdgeTpuDetector.__new__(EdgeTpuDetector)  # skip delegate/model loading
        d._in = {"dtype": dtype}
        return d

    def test_uint8_raw_pixels(self):
        frame = np.array([[[0, 128, 255]]], dtype=np.uint8)
        q = self._det(np.uint8)._prepare_input(frame)
        self.assertEqual(q.shape, (1, 1, 1, 3))
        np.testing.assert_array_equal(q[0, 0, 0], [0, 128, 255])

    def test_int8_shifted(self):
        frame = np.array([[[0, 255]]], dtype=np.uint8)
        q = self._det(np.int8)._prepare_input(frame)
        self.assertEqual(q.dtype, np.int8)
        np.testing.assert_array_equal(q[0, 0, 0], [-128, 127])

    def test_float_normalized(self):
        frame = np.array([[[255]]], dtype=np.uint8)
        q = self._det(np.float32)._prepare_input(frame)
        self.assertAlmostEqual(float(q[0, 0, 0, 0]), 1.0)

@unittest.skipIf(detectors is None, f"detectors unavailable ({_IMPORT_ERR})")
class TestAnchorFree(unittest.TestCase):
    """YOLOv8/v9 [1,4+nc,N] output: xywh + class scores, no objectness."""

    def test_layout_detection(self):
        self.assertEqual(postprocess.layout([[1, 84, 2100]], 80), "anchor_free")
        self.assertEqual(postprocess.layout([[1, 25200, 85]], 80), "ultralytics")
        self.assertEqual(postprocess.layout([[1, 255, 80, 80]] * 3, 80), "raw_heads")

    def test_duplicate_suppressed_and_score_is_class_conf(self):
        out = np.zeros((1, 84, 4), dtype=np.float32)
        for col in (0, 1):  # two identical class-2 (car) boxes
            out[0, :4, col] = [160, 160, 40, 20]
            out[0, 4 + 2, col] = 0.8
        out[0, 4 + 2, 2] = 0.3  # below OBJ_THRESH
        boxes, classes, scores = postprocess.post_process_anchor_free(out)
        self.assertEqual(len(boxes), 1)
        self.assertEqual(int(classes[0]), 2)
        self.assertAlmostEqual(float(scores[0]), 0.8, places=5)
        self.assertEqual([round(float(v), 3) for v in boxes[0]], [140.0, 150.0, 180.0, 170.0])

    def test_empty(self):
        boxes, _, _ = postprocess.post_process_anchor_free(np.zeros((1, 84, 5), np.float32))
        self.assertIsNone(boxes)


@unittest.skipIf(detectors is None, f"detectors unavailable ({_IMPORT_ERR})")
class TestCropDetection(unittest.TestCase):
    """A 320x320 anchor-free model: any frame or region crop is resized to it, and its
    boxes come back in that frame's pixels (what regions.py's crops need)."""

    def _detector(self):
        import types
        seen = {}

        class _In:
            name, type, shape = "images", "tensor(float)", [1, 3, 320, 320]

        class _Out:
            shape = [1, 84, 1]

        class _Session:
            def __init__(self, path, so, providers):
                self.providers = providers

            def get_providers(self):
                return self.providers

            def get_inputs(self):
                return [_In()]

            def get_outputs(self):
                return [_Out()]

            def run(self, _names, feed):
                seen["shape"] = feed["images"].shape
                out = np.zeros((1, 84, 1), np.float32)
                out[0, :4, 0] = [100, 50, 20, 10]  # model-space xywh
                out[0, 4 + 0, 0] = 0.9  # person
                return [out]

        class _Opts:
            def add_session_config_entry(self, k, v):
                pass

        fake = types.SimpleNamespace(InferenceSession=_Session, SessionOptions=_Opts,
                                     get_available_providers=lambda: ["CPUExecutionProvider"])
        with mock.patch.dict(sys.modules, {"onnxruntime": fake}):
            det = cpu.CpuDetector()
        return det, seen

    def test_input_size_and_layout_from_graph(self):
        det, _ = self._detector()
        self.assertEqual((det.input_size, det._mode), (320, "anchor_free"))

    def test_frame_resized_and_boxes_scaled_to_it(self):
        det, seen = self._detector()
        res = det.detect(np.zeros((640, 640, 3), np.uint8))
        self.assertEqual(seen["shape"], (1, 3, 320, 320))
        self.assertEqual((res[0]["objectType"], res[0]["label"]), ("person", "person"))
        # model xyxy (90,45,110,55) at 320 -> (180,90,220,110) at 640
        self.assertEqual([round(v, 3) for v in res[0]["box"]], [180.0, 90.0, 220.0, 110.0])

    def test_non_square_crop_scales_each_axis(self):
        det, _ = self._detector()
        res = det.detect(np.zeros((160, 640, 3), np.uint8))   # 640 wide, 160 high
        self.assertEqual([round(v, 3) for v in res[0]["box"]], [180.0, 22.5, 220.0, 27.5])

    def test_min_score(self):
        det, _ = self._detector()
        self.assertEqual(det.detect(np.zeros((320, 320, 3), np.uint8), min_score=0.95), [])

    def test_resize_without_opencv(self):
        frame = np.arange(4 * 4 * 3, dtype=np.uint8).reshape(4, 4, 3)
        with mock.patch.dict(sys.modules, {"cv2": None}):
            out = postprocess.resize(frame, 2)
        self.assertEqual(out.shape, (2, 2, 3))
        self.assertTrue((out[1, 1] == frame[2, 2]).all())


if __name__ == "__main__":
    unittest.main()
