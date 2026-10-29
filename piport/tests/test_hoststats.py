import os
import plistlib
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hoststats  # noqa: E402

# ps -A -o pid=,ppid=,time=,args= on macOS (hundredths) and on Linux (whole seconds, days)
MAC_PS = """\
    1     0   1:02.50 /sbin/launchd
  500     1   0:10.00 /opt/homebrew/bin/python3 piport/run_all.py --iface en0
  501   500   2:00.00 /opt/homebrew/bin/python3 piport/avclient.py --iface en0
  502   501   1:00.00 ffmpeg -loglevel warning -rtsp_transport tcp -i rtsp://x
  600     1   0:01.00 /Applications/Fregata.app/Contents/MacOS/Fregata
  601   600   3:00.00 frigate.detector:coreml
  700     1   0:30.00 /Applications/OrbStack.app/Contents/Frameworks/OrbStack Helper.app/Contents/MacOS/OrbStack Helper vmgr
  800     1   0:05.00 /usr/libexec/WindowServer
"""
LINUX_PS = """\
    1     0 00:00:09 /sbin/init
  900     1 1-02:00:00 /usr/bin/containerd-shim-runc-v2 -namespace moby -id abc
  901   900 00:10:00 python3 -m frigate
  950     1 00:00:30 /usr/bin/containerd-shim-runc-v2 -namespace moby -id def
  951   950 00:01:00 python3 unifi-cam-proxy
"""


class TestProcesses(unittest.TestCase):
    def test_cputime_formats(self):
        self.assertAlmostEqual(hoststats.parse_cputime("1:02.50"), 62.5)
        self.assertAlmostEqual(hoststats.parse_cputime("00:10:00"), 600)
        self.assertAlmostEqual(hoststats.parse_cputime("1-02:00:00"), 93600)

    def test_groups_on_macos(self):
        groups = hoststats.group_of(hoststats.parse_ps(MAC_PS), own_root=500)
        self.assertEqual([groups.get(p, "other") for p in (500, 501, 502, 600, 601, 700, 800)],
                         ["pi-port"] * 3 + ["Frigate"] * 2 + ["OrbStack", "other"])

    def test_groups_on_linux(self):
        # Frigate's container is named by its process; the camera proxy's is just a container.
        groups = hoststats.group_of(hoststats.parse_ps(LINUX_PS), own_root=12345)
        self.assertEqual(groups[901], "Frigate")
        self.assertEqual(groups[951], "containers")

    def test_cpu_over_the_interval(self):
        before = hoststats.parse_ps(MAC_PS)
        after = dict(before)
        after[501] = (500, before[501][1] + 30, before[501][2])   # 30 s of CPU in 60 s
        after[601] = (600, before[601][1] + 60, before[601][2])
        after[999] = (1, 6.0, "new")                                # started meanwhile
        del after[800]                                              # ended meanwhile
        cores = hoststats.cpu_by_group(before, after, 500, 60.0)
        self.assertEqual((cores["pi-port"], cores["Frigate"], cores["other"]), (0.5, 1.0, 0.1))


class TestMacPower(unittest.TestCase):
    def test_powermetrics_plist(self):
        sample = {"processor": {"cpu_power": 4100.0, "gpu_power": 1200.0, "ane_power": 800.0,
                                "clusters": [{"cpus": [{"idle_ratio": 0.5}, {"idle_ratio": 0.7}]}]},
                  "gpu": {"idle_ratio": 0.75}}
        data = plistlib.dumps(sample) + b"\0"
        self.assertEqual(hoststats.parse_powermetrics(data),
                         {"cpu_w": 4.1, "gpu_w": 1.2, "ane_w": 0.8, "cpu_busy": 0.4,
                          "gpu_busy": 0.25})

    def test_missing_keys_are_left_out(self):
        self.assertEqual(hoststats.parse_powermetrics(plistlib.dumps({"processor": {}})), {})


class TestLinux(unittest.TestCase):
    def test_proc_stat(self):
        self.assertEqual(hoststats.parse_proc_stat("cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 1 2 3\n"),
                         (150, 1000))

    def test_rk3588_npu(self):
        self.assertAlmostEqual(hoststats.parse_rknpu_load(
            "NPU load:  Core0: 12%, Core1:  0%, Core2:  3%,\n"), 0.05)
        self.assertIsNone(hoststats.parse_rknpu_load("nothing"))

    def test_devfreq_gpu(self):
        self.assertAlmostEqual(hoststats.parse_devfreq_load("37@800000000Hz\n"), 0.37)


class TestLine(unittest.TestCase):
    def test_format(self):
        line = hoststats.format_line(60, (4.1, 5.0, 5.7), {"pi-port": 1.75, "Frigate": 0.6,
                                                           "other": 0.81}, 8,
                                     {"cpu_busy": 0.42, "gpu_busy": 0.2, "cpu_w": 4.1,
                                      "ane_w": 0.8})
        self.assertEqual(line, "host over 60 s: load 4.1/5.0/5.7, CPU 3.2 of 8 cores (pi-port "
                               "1.75, Frigate 0.60, other 0.81), cores 42% busy, GPU 20% busy, "
                               "power CPU 4.1 W, ANE 0.8 W")



class TestWithoutPs(unittest.TestCase):
    def test_no_ps_gives_no_process_groups(self):
        from unittest import mock
        with mock.patch.object(hoststats.subprocess, "run", side_effect=FileNotFoundError("ps")):
            self.assertEqual(hoststats.read_processes(), {})

if __name__ == "__main__":
    unittest.main()
