# SPDX-License-Identifier: AGPL-3.0-or-later
"""replay.py on synthetic clips (a white box crossing a black frame) with a fake detector
that finds it, and record_clip.py's log scan. ffmpeg tests are skipped if it's absent."""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import time
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("AIPORT_TICK_S", "0.33")       # tests assume macOS's tick and
os.environ.setdefault("AIPORT_DETECT_FPS", "10")    # detection rate on every platform
import avclient as av  # noqa: E402
import replay  # noqa: E402

_FFMPEG = shutil.which(av.FFMPEG_BIN)
_CV2 = importlib.util.find_spec("cv2") is not None   # the Frigate motion gate, regions, stationary
_spec = importlib.util.spec_from_file_location(
    "record_clip", os.path.join(os.path.dirname(_ROOT), "scripts", "record_clip.py"))
record_clip = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(record_clip)


def _time_now():
    return time.time()


class BrightBoxDetector:
    """Finds the white pixels in a frame and reports them as one person."""

    def detect(self, frame, min_score=None):
        import numpy as np
        ys, xs = np.nonzero(frame[:, :, 0] > 200)
        if len(xs) < 20:
            return []
        return [{"objectType": "person", "label": "person", "score": 0.9,
                 "box": (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))}]


def make_clip(path, enable="between(t,0.5,2.5)", duration=3):
    """A 320x180 black clip with a 40x60 white box moving right while `enable` holds."""
    subprocess.run(
        [_FFMPEG, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         f"color=black:s=320x180:r=25:d={duration}[bg];color=white:s=40x60:r=25:d={duration}[fg];"
         f"[bg][fg]overlay=x='20+t*60':y=60:enable='{enable}'",
         "-c:v", "mjpeg", "-q:v", "3", path], check=True)


@unittest.skipUnless(_FFMPEG, "ffmpeg not installed")
class TestReplay(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.clip = os.path.join(self.dir, "walk.avi")
        make_clip(self.clip)
        patcher = mock.patch.object(av, "_detector", BrightBoxDetector())
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_replay(self, settings=None):
        return replay.replay(av, replay.clip_frames(av, self.clip), settings or {"width": 320,
                                                                                 "height": 180})

    @unittest.skipUnless(_CV2, "OpenCV not installed: the average-diff gate misses the test box")
    def test_crossing_box_is_one_event(self):
        sent = self.run_replay()
        edges = [p["edgeType"] for _, p in sent]
        self.assertEqual(edges[0], "enter")
        self.assertEqual(edges[-1], "leave")
        self.assertEqual(set(edges[1:-1]), {"moving"})
        events = replay.summarize(sent)
        self.assertEqual(len(events), 1)
        self.assertEqual([o["type"] for o in events[0]["objects"]], ["person"])
        # it appears at 0.5 s and is sent once it has initialised (MIN_INITIALIZED frames
        # later), its enter dated back to 0.5 s; its track closes AIPORT_MISS_S after it goes
        # at 2.5 s
        init_s = (av.MIN_INITIALIZED + 1) / av.DETECT_FPS
        self.assertAlmostEqual(events[0]["start_s"], 0.5 + init_s, delta=0.3)
        walls = [p["clockWall"] for _, p in sent]
        self.assertAlmostEqual((walls[1] - walls[0]) / 1000.0, av.REPORT_INTERVAL_MS / 1000.0,
                               delta=0.15)   # back-filled from the first frame
        self.assertAlmostEqual(events[0]["end_s"], 2.5 + av.MOTION_MISS_TOLERANCE_S, delta=0.5)

    @unittest.skipUnless(_CV2, "OpenCV not installed: the average-diff gate misses the test box")
    def test_boxes_move_right(self):
        sent = self.run_replay()
        xs = [p["descriptors"][0]["coord"][0] for _, p in sent if p["edgeType"] == "moving"]
        self.assertGreaterEqual(len(xs), 3)  # one update per AIPORT_TICK_S
        self.assertGreater(xs[-1], xs[0])

    def test_detection_zone_elsewhere_means_no_event(self):
        settings = {"width": 320, "height": 180, "smartDetect": {
            "zones": {"1": {"coord": [0, 900, 100, 900, 100, 1000, 0, 1000]}}}}
        self.assertEqual(self.run_replay(settings), [])

    @unittest.skipUnless(_CV2, "OpenCV not installed: the average-diff gate misses the test box")
    def test_event_start_msec_from_sidecar(self):
        settings = {"width": 320, "height": 180, "smartDetect": {"eventStartMSec": 1000}}
        events = replay.summarize(self.run_replay(settings))
        self.assertAlmostEqual(events[0]["start_s"], 1.5, delta=0.3)

    def test_avclient_left_as_it_was(self):
        before = (av.send_msg, av.LEAVE_FALLBACK_S, av.STARTUP_WINDOW_MS, av._STREAM_DIR)
        self.run_replay()
        self.assertEqual((av.send_msg, av.LEAVE_FALLBACK_S, av.STARTUP_WINDOW_MS,
                          av._STREAM_DIR), before)
        self.assertNotIn(replay.DEVICE, av._motion_state)

    @unittest.skipUnless(_CV2, "OpenCV not installed: the average-diff gate misses the test box")
    def test_cli_json_with_sidecar(self):
        with open(os.path.join(self.dir, "walk.json"), "w") as f:
            json.dump({"width": 320, "height": 180, "start": "2026-10-17T13:54:00-04:00"}, f)
        out = os.path.join(self.dir, "out.json")
        with open(out, "w") as f, mock.patch("sys.stdout", f):
            replay.main([self.clip, "--json"])
        with open(out) as f:
            summary = json.load(f)
        self.assertEqual(len(summary["events"]), 1)
        self.assertGreater(summary["messages"], 2)

    def test_faces_read_from_full_size_frames_once_a_second(self):
        reads = []
        with mock.patch.object(av, "_enrich_detections",
                               lambda dev, dets: reads.append(av._load_snapshot_bgr(dev)) or []):
            replay.replay(av, replay.clip_frames(av, self.clip), {"width": 320, "height": 180},
                          snapshots=replay.snapshot_frames(av, self.clip))
        shots = [(img.shape, t) for img, t in reads if img is not None]
        self.assertTrue(shots)
        self.assertEqual({shape for shape, _ in shots}, {(180, 320, 3)})   # the clip's own size
        self.assertEqual(sorted({t for _, t in shots}), [0.0, 1.0, 2.0])   # one a second

    def test_check_clips_runs_the_sidecar_expectations(self):
        with open(os.path.join(self.dir, "walk.json"), "w") as f:
            json.dump({"expect": {"events": 1, "objects": {"person": 1}},
                       "env": {"AIPORT_TICK_S": "0.2"}, "args": ["--device", "D1"]}, f)
        runs = []
        def run(cmd, env, timeout):
            runs.append((cmd, env))
            events = [{"objects": [{"tracker": 1, "type": "person"}]}]
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"events": events}), "")
        out = io.StringIO()
        with mock.patch.object(replay, "_run_clip", run):
            self.assertEqual(replay.check_clips([self.dir], out=out), 0)
            (cmd, env), = runs
            self.assertEqual(cmd[2:], [self.clip, "--json", "--device", "D1"])
            self.assertEqual(env["AIPORT_TICK_S"], "0.2")
            self.assertIn("PASS walk.avi", out.getvalue())
            with open(os.path.join(self.dir, "walk.json"), "w") as f:
                json.dump({"expect": {"events": 2}, "why": "two walks"}, f)
            self.assertEqual(replay.check_clips([self.dir], out=out), 1)
        self.assertIn("FAIL walk.avi", out.getvalue())
        self.assertIn("guards: two walks", out.getvalue())

    def test_a_hung_clip_times_out_and_fails(self):
        # Live: one clip ran 21 min at 74% CPU and the whole run printed nothing.
        with open(os.path.join(self.dir, "walk.json"), "w") as f:
            json.dump({"expect": {"events": 0}}, f)
        limits = []
        def run(cmd, env, timeout):
            limits.append(timeout)
            raise subprocess.TimeoutExpired(cmd, timeout)
        out = io.StringIO()
        with mock.patch.object(replay, "_run_clip", run):
            self.assertEqual(replay.check_clips([self.dir], out=out, timeout=7), 1)
            self.assertIn("FAIL walk.avi: timed out after 7 s", out.getvalue())
            with open(os.path.join(self.dir, "walk.json"), "w") as f:
                json.dump({"expect": {"events": 0}, "timeout": 90}, f)
            replay.check_clips([self.dir], out=out, timeout=7)
        self.assertEqual(limits, [7, 90])   # a sidecar's "timeout" wins

    def test_sweep_removes_dirs_of_dead_replays_only(self):
        # A replay killed by --check's timeout never ran its finally: its temp dirs stayed.
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        names = ["piport-replay-%d-stream-a" % dead.pid, "piport-replay-%d-state-b" % os.getpid(),
                 "piport-replay-0y6kuh8l", "piport-replay-state-ilec8hea", "other-dir"]
        for n in names:
            os.mkdir(os.path.join(tmp, n))
        old = _time_now() - 2 * replay.LEGACY_DIR_MAX_AGE_S
        os.utime(os.path.join(tmp, "piport-replay-0y6kuh8l"), (old, old))
        removed = replay.sweep_replay_dirs(tmp)
        self.assertEqual(sorted(removed), sorted([names[0], names[2]]))
        self.assertEqual(sorted(os.listdir(tmp)), sorted(names[1:2] + names[3:]))   # live, new

    def test_run_clip_kills_the_whole_process_group(self):
        # The replay's ffmpeg was left running after only the replay was killed.
        cmd = [sys.executable, "-c", "import subprocess, sys, time; subprocess.Popen("
               "[sys.executable, '-c', 'import time; time.sleep(30)']); time.sleep(30)"]
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            replay._run_clip(cmd, dict(os.environ), 1)
        self.assertLess(time.monotonic() - t0, 10)   # didn't wait for the grandchild
        done = replay._run_clip([sys.executable, "-c", "print('ok')"], dict(os.environ), 10)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"))

class TestCheck(unittest.TestCase):
    """replay.check: what a clip's summary misses of its "expect" block."""

    def ev(self, *objects):
        return {"objects": [dict(o) for o in objects]}

    def test_counts(self):
        events = [self.ev({"tracker": 1, "type": "vehicle"}, {"tracker": 2, "type": "vehicle"})]
        self.assertEqual(replay.check({"events": 1, "objects": {"vehicle": [1, 2]}}, events), [])
        self.assertEqual(replay.check({"objects": {"vehicle": 1}}, events),
                         ["2 vehicle track(s), expected 1"])
        self.assertEqual(replay.check({"events": 0}, events), ["1 event(s), expected 0"])

    def test_faces_sharing_a_thumbnail(self):
        # 13:33 before the fix: two face tracks both kept her face as their thumbnail.
        events = [self.ev({"tracker": 9, "type": "face", "snapshot": [510, 525, 37, 85]},
                          {"tracker": 10, "type": "face", "snapshot": [511, 525, 37, 85]})]
        self.assertEqual(len(replay.check({"distinct_face_snapshots": True}, events)), 1)
        events[0]["objects"][1]["snapshot"] = [684, 276, 21, 53]
        self.assertEqual(replay.check({"distinct_face_snapshots": True}, events), [])

    def test_vehicle_attributes(self):
        # 15:13 Camera D: a black SUV went out as car/truck and blue.
        events = [self.ev({"tracker": 3, "type": "vehicle", "vehicle_type": "truck", "color": "blue"},
                          {"tracker": 5, "type": "vehicle", "vehicle_type": "suv", "color": "black"})]
        self.assertEqual(replay.check({"vehicle_type": "suv", "color": "black"}, events),
                         ["vehicle #3 vehicle type truck, expected suv",
                          "vehicle #3 color blue, expected black"])
        self.assertEqual(replay.check({"vehicle_type": ["suv", "truck"]}, events), [])
        self.assertEqual(replay.check({"color": "black"}, [self.ev()]),
                         ["no vehicle to check color against"])

    def test_face_embed_sent(self):
        events = [self.ev({"tracker": 44, "type": "face", "face_embed": True})]
        self.assertEqual(replay.check({"no_face_embed": True}, events),
                         ["faceEmbed sent for face(s) [44]"])

    def test_summary_records_thumbnails_and_embeds(self):
        sent = [(1.0, {"eventId": 7, "edgeType": "enter", "descriptors": [
                    {"trackerID": 4, "objectType": "face", "confidenceLevel": 80}]}),
                (2.0, {"eventId": 7, "edgeType": "leave", "descriptors": [],
                       "smartDetectSnapshots": [{"trackerID": 4, "smartDetectSnapshotType": "face",
                                                 "coord": [1, 2, 3, 4], "confidenceLevel": 90}],
                       "trackerIDAttrMap": {"4": {"objectType": "face", "faceEmbed": [0.1]}}})]
        (face,) = replay.summarize(sent)[0]["objects"]
        self.assertEqual((face["snapshot"], face["face_embed"]), ([1, 2, 3, 4], True))

    def test_summary_records_vehicle_attributes(self):
        sent = [(2.0, {"eventId": 8, "edgeType": "leave", "descriptors": [
                    {"trackerID": 3, "objectType": "vehicle", "confidenceLevel": 80}],
                       "trackerIDAttrMap": {"3": {"objectType": "vehicle",
                                                  "vehicleType": {"val": "suv", "confidence": 70},
                                                  "color": {"val": "black", "confidence": 40},
                                                  "matchedName": "ABCD123"}}})]
        (car,) = replay.summarize(sent)[0]["objects"]
        self.assertEqual((car["vehicle_type"], car["color"], car["plate"]), ("suv", "black", "ABCD123"))


class TestRecordClipLogScan(unittest.TestCase):
    LOG = [
        "2026-10-17 15:09:59,455 INFO starting merged RTSP pull for deviceID=02000000000A: "
        "rtsp://192.0.2.1:7447/old (codec=hevc)\n",
        "2026-10-17 15:10:01,607 INFO <<< received functionName='UiStreamControl' "
        "messageId=1 from='UniFiVideo' to='ubnt_avclient' payload={'streaming': True, "
        "'ip': '192.0.2.1', 'port': '7447', 'uri': 'x', 'deviceID': '02000000000A', "
        "'width': 1920, 'height': 2560, 'fps': 24}\n",
        "2026-10-17 15:10:02,000 INFO <<< received functionName='ChangeSmartDetectSettings' "
        "messageId=2 from='UniFiVideo' to='ubnt_avclient' payload={'enableSmartDetect': "
        "['person'], 'eventStartMSec': 1000, 'zones': {'1': {'coord': [0, 0, 1000, 0, 1000, "
        "1000, 0, 1000]}}, 'excludeZones': {}, 'deviceID': '02000000000A'}\n",
        "2026-10-17 15:10:02,100 INFO <<< received functionName='ChangeSmartDetectSettings' "
        "messageId=3 from='UniFiVideo' to='ubnt_avclient' payload={'isLprCamera': 0, "
        "'deviceID': '02000000000A'}\n",
        "2026-10-17 15:10:02,200 INFO <<< received functionName='ChangeSmartMotionSettings' "
        "messageId=4 from='UniFiVideo' to='ubnt_avclient' payload={'eventMaxDurationMSec': "
        "300000, 'deviceID': '02000000000A'}\n",
        "2026-10-17 15:20:00,000 INFO starting merged RTSP pull for deviceID=02000000000A: "
        "rtsp://192.0.2.1:7447/new (codec=hevc)\n",
        "2026-10-17 15:20:00,000 INFO starting merged RTSP pull for deviceID=02000000000C: "
        "rtsp://192.0.2.1:7447/other (codec=hevc)\n",
    ]

    def test_latest_values_for_the_camera(self):
        info = record_clip.scan_log(self.LOG, "02000000000A")
        self.assertEqual(info["url"], "rtsp://192.0.2.1:7447/new")
        self.assertEqual((info["width"], info["height"], info["fps"]), (1920, 2560, 24))
        self.assertEqual(info["smartDetect"]["eventStartMSec"], 1000)  # not the partial one
        self.assertEqual(info["smartMotion"]["eventMaxDurationMSec"], 300000)

    def test_unknown_camera(self):
        self.assertEqual(record_clip.scan_log(self.LOG, "000000000000"), {})


if __name__ == "__main__":
    unittest.main()
