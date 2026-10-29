import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("AIPORT_TICK_S", "0.33")       # tests assume macOS's tick and
os.environ.setdefault("AIPORT_DETECT_FPS", "10")    # detection rate on every platform
import apple_fm  # noqa: E402
import avclient as av  # noqa: E402


class TestParseAnswer(unittest.TestCase):
    def test_protect_vocabulary(self):
        self.assertEqual(apple_fm.parse_answer("suv black"), ("suv", "black"))
        self.assertEqual(apple_fm.parse_answer("Sedan, Grey."), ("car", "gray"))
        self.assertEqual(apple_fm.parse_answer("pickup silver"), ("truck", "gray"))
        self.assertEqual(apple_fm.parse_answer("minivan white"), ("van", "white"))

    def test_camper_is_a_trailer(self):
        # The model called the parked camper a "van" until trailer was an allowed answer.
        self.assertEqual(apple_fm.parse_answer("camper brown"), ("trailer", "brown"))

    def test_unknown_words(self):
        self.assertEqual(apple_fm.parse_answer("I can't tell"), (None, None))
        self.assertEqual(apple_fm.parse_answer(""), (None, None))


class TestAsSessionUser(unittest.TestCase):
    def test_root_runs_it_as_the_sudo_user(self):
        with mock.patch.object(apple_fm.os, "geteuid", return_value=0), \
                mock.patch.dict(os.environ, {"SUDO_UID": "501", "SUDO_USER": "me"}):
            self.assertEqual(apple_fm._as_session_user(["helper"]),
                             ["launchctl", "asuser", "501", "sudo", "-u", "me", "--", "helper"])

    def test_a_user_runs_it_directly(self):
        with mock.patch.object(apple_fm.os, "geteuid", return_value=501):
            self.assertEqual(apple_fm._as_session_user(["helper"]), ["helper"])


class TestVote(unittest.TestCase):
    def test_weighted_by_box_area(self):
        # Live 15:13: a black SUV's crops came back truck, suv, suv.
        votes = [("truck", "black", 100), ("suv", "black", 150), ("suv", "black", 200)]
        self.assertEqual(av._fm_vote(votes),
                         {"vehicleType": {"val": "suv", "confidence": 78},
                          "color": {"val": "black", "confidence": 100}})

    def test_trailer_drops_the_type(self):
        self.assertIsNone(av._fm_vote([("trailer", "gray", 1)])["vehicleType"])

    def test_unanswered_field_left_out(self):
        self.assertEqual(av._fm_vote([(None, "red", 1)]), {"color": {"val": "red", "confidence": 100}})


class TestWhatIsSent(unittest.TestCase):
    """Boxes carry a type/colour only once 2 answers agree; a leave carries the vote."""

    PER_FRAME = {"vehicleType": {"val": "car", "confidence": 70}, "color": {"val": "blue", "confidence": 40}}

    def setUp(self):
        for name, value in (("VEHICLE_FM", True), ("_fm_classifier", None)):
            patcher = mock.patch.object(av, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def track(self, *votes):
        return {"objectType": "vehicle", "extra": {"attributes": dict(self.PER_FRAME)},
                "fm_votes": list(votes)}

    def test_box_shows_nothing_until_two_answers_agree(self):
        # Live 16:11: a bus's first, dark crop said black; the box kept it until the leave.
        tr = self.track(("bus", "black", 100))
        self.assertEqual(av._vehicle_attrs(tr, leaving=False), {})
        tr["fm_votes"].append(("bus", "yellow", 150))
        self.assertEqual(av._vehicle_attrs(tr, leaving=False),
                         {"vehicleType": {"val": "bus", "confidence": 100}})   # colours differ
        tr["fm_votes"].append(("bus", "yellow", 150))
        self.assertEqual(av._vehicle_attrs(tr, leaving=False)["color"]["val"], "yellow")

    def test_leave_always_carries_the_vote(self):
        tr = self.track(("suv", "black", 100))
        self.assertEqual(av._vehicle_attrs(tr, leaving=True),
                         {"vehicleType": {"val": "suv", "confidence": 100},
                          "color": {"val": "black", "confidence": 100}})

    def test_no_answer_leaves_the_per_frame_guess_on_the_leave_only(self):
        tr = self.track()
        self.assertEqual(av._vehicle_attrs(tr, leaving=True), self.PER_FRAME)
        self.assertEqual(av._vehicle_attrs(tr, leaving=False), {})

    def test_without_the_model_the_per_frame_guess_stays(self):
        with mock.patch.object(av, "_fm_classifier", mock.Mock(available=False)):
            self.assertEqual(av._vehicle_attrs(self.track(), leaving=False), self.PER_FRAME)

    def test_track_args_use_it(self):
        tr = dict(self.track(("suv", "black", 1)), objectType="vehicle", coord=[1, 2, 3, 4],
                  score=0.9, first_shown_ms=0)
        self.assertEqual(av._track_args(3, tr)["descriptor_extra"]["attributes"], {})
        leave = av._track_args(3, tr, hold_embed=False)["descriptor_extra"]["attributes"]
        self.assertEqual(leave["vehicleType"]["val"], "suv")


class TestEdgeAndHold(unittest.TestCase):
    def test_cut_off_crop_counts_a_quarter(self):
        # Live 16:11 on Camera B: the bus cut off at the left edge said van, the next crop bus.
        tr = {"objectType": "vehicle", "fm_pending": 2}
        with mock.patch.object(av, "_fm_classify", side_effect=[(("van", "yellow", 1), 400),
                                                                (("bus", "yellow", 1), 300)]), \
                mock.patch.object(av, "_fm_remember"):
            av._fm_job("D", tr, b"x", [0, 1, 139, 194], locked=True)
            av._fm_job("D", tr, b"x", [300, 100, 120, 150], locked=True)
        self.assertEqual(av._fm_vote(tr["fm_votes"])["vehicleType"]["val"], "bus")
        self.assertEqual(tr["fm_pending"], 0)

    def test_close_waits_for_a_pending_answer(self):
        tr = {"fm_pending": 1}
        self.assertTrue(av._fm_hold(tr, 1000))
        self.assertTrue(av._fm_hold(tr, 3400))
        self.assertFalse(av._fm_hold(tr, 3600))        # 2.5 s is the limit
        self.assertFalse(av._fm_hold({"fm_pending": 0}, 1000))


class TestRequest(unittest.TestCase):
    def setUp(self):
        self.jobs = []
        self.now = [1_000_000]
        for name, value in (("VEHICLE_FM_SYNC", True), ("_fm_classifier", None), ("VEHICLE_FM", True),
                            ("_fm_job", lambda *a, **k: self.jobs.append(a)),
                            ("_frame_wall_ms", lambda dev: self.now[0])):
            patcher = mock.patch.object(av, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        av._stream_dims["D"] = (2560, 1440)
        self.addCleanup(av._stream_dims.pop, "D", None)

    def track(self, coord=(500, 300, 100, 150)):
        return {"objectType": "vehicle", "best": {"jpeg": b"x", "coord": list(coord)}}

    def test_at_most_three_crops_spaced_out(self):
        tr = self.track()
        for step in range(8):
            av._request_fm("D", tr)
            self.now[0] += 1000          # every second: only every other one is far enough apart
        self.assertEqual(len(self.jobs), 3)

    def test_too_small_or_cut_off_is_not_sent(self):
        av._request_fm("D", self.track((500, 300, 30, 150)))   # 77 px wide
        av._request_fm("D", self.track((707, 43, 94, 62)))     # 89 px high: the roof at the edge
        self.assertEqual(self.jobs, [])

    def test_vehicle_in_the_same_spot_reuses_the_answers(self):
        # Live: a trailer parked at Camera B was re-asked about every few minutes.
        av._fm_cache.pop("D", None)
        self.addCleanup(av._fm_cache.pop, "D", None)
        av._fm_remember("D", [500, 300, 100, 150], [("trailer", "black", 1)])
        tr = self.track((505, 300, 100, 150))
        av._request_fm("D", tr)
        self.assertEqual(self.jobs, [])
        self.assertEqual(tr["fm_votes"], [("trailer", "black", 1)])
        self.now[0] += av.VEHICLE_FM_REUSE_MS + 1               # too long ago: asked again
        av._request_fm("D", self.track((505, 300, 100, 150)))
        self.assertEqual(len(self.jobs), 1)

    def test_nothing_once_the_model_is_unavailable(self):
        with mock.patch.object(av, "_fm_classifier", mock.Mock(available=False)):
            av._request_fm("D", self.track())
        self.assertEqual(self.jobs, [])


if __name__ == "__main__":
    unittest.main()
