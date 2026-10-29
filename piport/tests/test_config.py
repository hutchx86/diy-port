# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for config.py's pure logic (no hardware, no network)."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import config  # noqa: E402


class TestNetmask(unittest.TestCase):
    def test_common_masks(self):
        self.assertEqual(config.netmask_to_prefix("255.255.255.0"), 24)
        self.assertEqual(config.netmask_to_prefix("255.255.0.0"), 16)
        self.assertEqual(config.netmask_to_prefix("255.0.0.0"), 8)
        self.assertEqual(config.netmask_to_prefix("255.255.255.255"), 32)

    def test_invalid(self):
        self.assertIsNone(config.netmask_to_prefix(""))
        self.assertIsNone(config.netmask_to_prefix("not-a-mask"))
        self.assertIsNone(config.netmask_to_prefix("255.255.255"))
        self.assertIsNone(config.netmask_to_prefix("255.0.255.0"))


class TestMisc(unittest.TestCase):
    def test_get_bool(self):
        for v in ("1", "true", "TRUE", "yes", "on", " On "):
            self.assertTrue(config.get_bool({"x": v}, "x"), v)
        for v in ("0", "false", "no", "off", ""):
            self.assertFalse(config.get_bool({"x": v}, "x"), v)
        self.assertFalse(config.get_bool({}, "missing"))

    def test_sysid_int(self):
        self.assertEqual(config.sysid_int({"sysid": "0xa5f1"}), 0xA5F1)
        self.assertEqual(config.sysid_int({"sysid": "42481"}), 42481)


class TestResolveIface(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(config.resolve_iface("ap-main", {"iface": "eth0"}), "ap-main")

    def test_cfg_wins(self):
        self.assertEqual(config.resolve_iface(None, {"iface": "enP4p65s0"}), "enP4p65s0")

    def test_blank_auto_detects(self):
        # auto-detect must return a string, never None (falls back to "eth0")
        self.assertIsInstance(config.resolve_iface(None, {"iface": ""}), str)
        self.assertIsInstance(config.resolve_iface(), str)

    def test_default_iface_is_str(self):
        self.assertIsInstance(config._default_iface(), str)

    def test_parent_precedence(self):
        self.assertEqual(config.resolve_parent_iface(
            {"parent_iface": "enP4p65s0", "iface": "eth0"}), "enP4p65s0")
        self.assertEqual(config.resolve_parent_iface(
            {"parent_iface": "", "iface": "enP4p65s0"}), "enP4p65s0")
        self.assertIsInstance(config.resolve_parent_iface({"parent_iface": "", "iface": ""}), str)


class TestBsdFallbacks(unittest.TestCase):
    """macOS/BSD has no /sys, /proc or `ip`: the interface, MAC and IP come
    from `route`, `networksetup` and `ifconfig` instead (stubbed here)."""

    IFCONFIG = ("en0: flags=8863<UP,BROADCAST,RUNNING> mtu 1500\n"
                "\tether c8:89:f3:d4:c3:cd\n"
                "\tinet6 fe80::1%en0 prefixlen 64 scopeid 0xe\n"
                "\tinet 192.0.2.10 netmask 0xffffff00 broadcast 192.0.2.255\n")

    def _outputs(self, **by_cmd):
        return lambda cmd: by_cmd.get(cmd[0], "")

    def test_default_iface_from_route(self):
        route = "   route to: default\ndestination: default\n  interface: en0\n"
        with mock.patch("config.open", side_effect=OSError, create=True), \
                mock.patch("config.os.listdir", side_effect=OSError), \
                mock.patch("config._run", self._outputs(route=route)):
            self.assertEqual(config._default_iface(), "en0")

    def test_mac_prefers_networksetup(self):
        # macOS masks ifconfig's ether as 02:00:00:00:00:00 for most processes.
        masked = self.IFCONFIG.replace("c8:89:f3:d4:c3:cd", "02:00:00:00:00:00")
        ns = "Ethernet Address: c8:89:f3:d4:c3:cd (Device: en0)\n"
        with mock.patch("config._run", self._outputs(networksetup=ns, ifconfig=masked)):
            self.assertEqual(config._iface_mac("nosuchif0"), "C889F3D4C3CD")

    def test_mac_from_ifconfig(self):
        with mock.patch("config._run", self._outputs(ifconfig=self.IFCONFIG)):
            self.assertEqual(config._iface_mac("nosuchif0"), "C889F3D4C3CD")

    def test_masked_mac_is_unknown(self):
        masked = self.IFCONFIG.replace("c8:89:f3:d4:c3:cd", "02:00:00:00:00:00")
        with mock.patch("config._run", self._outputs(ifconfig=masked)):
            self.assertEqual(config._iface_mac("nosuchif0"), "")

    def test_ip_from_ifconfig(self):
        with mock.patch("config.subprocess.run", side_effect=OSError), \
                mock.patch("config._run", self._outputs(ifconfig=self.IFCONFIG)):
            self.assertEqual(config._iface_ip("en0"), "192.0.2.10")

    def test_busybox_inet_addr_rejected(self):
        busybox = "eth0  Link encap:Ethernet\n  inet addr:10.0.0.5  Bcast:10.0.0.255\n"
        with mock.patch("config.subprocess.run", side_effect=OSError), \
                mock.patch("config._run", self._outputs(ifconfig=busybox)):
            self.assertIsNone(config._iface_ip("eth0"))


class TestStreamDir(unittest.TestCase):
    def test_default_is_ram_backed(self):
        d = config.stream_dir()
        # falls back to "default" key; must be an absolute path, never the state dir
        self.assertTrue(os.path.isabs(d))
        self.assertEqual(config.stream_dir(), d)

    def test_per_state_dir_is_unique_and_stable(self):
        a = config.stream_dir("/srv/x/instances/main")
        b = config.stream_dir("/srv/x/instances/two")
        self.assertNotEqual(a, b)
        self.assertEqual(config.stream_dir("/srv/x/instances/main"), a)
        # relative vs absolute of the same state dir must agree
        # realpath: /tmp is a symlink to /private/tmp on macOS
        tmp = os.path.realpath(tempfile.gettempdir())
        old_cwd = os.getcwd()
        os.chdir(tmp)
        self.addCleanup(os.chdir, old_cwd)
        self.assertEqual(config.stream_dir("instances/main"),
                         config.stream_dir(os.path.join(tmp, "instances/main")))

    def test_tmpfs_base_writable(self):
        self.assertTrue(os.path.isdir(config._tmpfs_base()))


class TestLoadConfig(unittest.TestCase):
    def test_defaults_and_override(self):
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as f:
            f.write("[identity]\nhostname = lab2\n[network]\niface = enP4p65s0\n")
            path = f.name
        try:
            cfg = config.load_config(path)
            self.assertEqual(cfg["hostname"], "lab2")
            self.assertEqual(cfg["iface"], "enP4p65s0")
            self.assertEqual(cfg["platform"], config.DEFAULTS["platform"])  # untouched key
        finally:
            os.unlink(path)

    def test_missing_file_falls_back(self):
        cfg = config.load_config("/nonexistent/path/aiport.cfg")
        self.assertEqual(cfg["platform"], "UVC AI Port")
        self.assertEqual(cfg["sysid"], "0xa5f1")

    def test_discovery_platform_is_the_sku(self):
        # Network's fingerprint catalogue knows the AI Port as "UP-AI-Port", not
        # the canonical Protect type "UVC AI Port".
        self.assertEqual(config.DEFAULTS["discovery_platform"], "UP-AI-Port")
        self.assertEqual(config.DEFAULTS["platform"], "UVC AI Port")

    def test_env_overrides_win_over_file(self):
        # Container-friendly override: env must beat a mounted cfg value, and a
        # blank env var must not clobber it.
        with tempfile.NamedTemporaryFile("w", suffix=".cfg", delete=False) as f:
            f.write("[console]\nhost = 10.0.0.1\n[identity]\nhostname = fromfile\n")
            path = f.name
        saved = os.environ.get("AIPORT_CONSOLE_HOST")
        self.addCleanup(lambda: os.environ.__setitem__("AIPORT_CONSOLE_HOST", saved)
                        if saved is not None else os.environ.pop("AIPORT_CONSOLE_HOST", None))
        try:
            os.environ["AIPORT_CONSOLE_HOST"] = "10.9.9.9"
            os.environ["AIPORT_HOSTNAME"] = ""  # blank -> file wins
            cfg = config.load_config(path)
            self.assertEqual(cfg["host"], "10.9.9.9")
            self.assertEqual(cfg["hostname"], "fromfile")
        finally:
            os.environ.pop("AIPORT_HOSTNAME", None)
            os.unlink(path)

    def test_env_overrides_model_identity(self):
        # sysid, type, discovery platform and guid from the env.
        env = {"AIPORT_SYSID": "0x1234", "AIPORT_PLATFORM": "Test Type",
               "AIPORT_DISCOVERY_PLATFORM": "Test SKU",
               "AIPORT_GUID": "11111111-2222-3333-4444-555555555555"}
        for k in env:
            saved = os.environ.get(k)
            self.addCleanup(lambda k=k, saved=saved: os.environ.__setitem__(k, saved)
                            if saved is not None else os.environ.pop(k, None))
        os.environ.update(env)
        cfg = config.load_config("/nonexistent/aiport.cfg")
        self.assertEqual(config.sysid_int(cfg), 0x1234)
        self.assertEqual(cfg["platform"], "Test Type")
        self.assertEqual(cfg["discovery_platform"], "Test SKU")
        self.assertEqual(cfg["guid"], env["AIPORT_GUID"])


class TestFirmwarePin(unittest.TestCase):
    """A simulated update pins a version; every stage reports it over the configured one."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.pin = os.path.join(self.d, "fw_version.json")

    def test_missing_falls_back_to_configured(self):
        self.assertIsNone(config.pinned_fw_version(self.d))
        self.assertEqual(config.effective_fw_version("5.1.12.67", self.d), "5.1.12.67")

    def test_round_trip(self):
        config.atomic_write_json(self.pin, {"version": "5.9.9.9"})
        self.assertEqual(config.pinned_fw_version(self.d), "5.9.9.9")
        self.assertEqual(config.effective_fw_version("5.1.12.67", self.d), "5.9.9.9")

    def test_malformed_or_empty_is_unpinned(self):
        with open(self.pin, "w") as f:
            f.write("{not json")
        self.assertIsNone(config.pinned_fw_version(self.d))
        config.atomic_write_json(self.pin, {})
        self.assertIsNone(config.pinned_fw_version(self.d))

    def test_device_name_default_and_round_trip(self):
        self.assertEqual(config.device_name(self.d), "AI Port")
        config.set_device_name(self.d, "Front Door")
        self.assertEqual(config.device_name(self.d), "Front Door")


class TestRedact(unittest.TestCase):
    def test_rtsp_credentials(self):
        line = ("payload={'streamUrl': 'rtsp://onvif:s3cret@192.168.1.22/Streaming/Channels/101"
                "?transportmode=unicast', 'width': 3840}")
        out = config.redact(line)
        self.assertNotIn("s3cret", out)
        self.assertNotIn("onvif:", out)
        self.assertIn("rtsp://<redacted>@192.168.1.22/Streaming/Channels/101", out)

    def test_password_and_token_fields(self):
        out = config.redact('body={"username": "ui", "password": "FAKEPASS", "token": "FAKE"}')
        self.assertEqual(out, 'body={"username": "ui", "password": "<redacted>", '
                              '"token": "<redacted>"}')
        self.assertEqual(config.redact("headers={'X-Guid': 'g', 'X-Token': 'FAKE'}"),
                         "headers={'X-Guid': 'g', 'X-Token': '<redacted>'}")
        out = config.redact("payload={'mac': 'fcecda', 'adoptionCode': 'FAKE', 'ip': '1.2.3.4'}")
        self.assertNotIn("FAKE", out)
        self.assertIn("'ip': '1.2.3.4'", out)

    def test_token_query_and_label(self):
        self.assertEqual(config.redact("wss://h:7442/camera/1.0/ws?token=abc123 headers"),
                         "wss://h:7442/camera/1.0/ws?token=<redacted> headers")
        self.assertEqual(config.redact("  token:      FAKETOKEN0123456"), "  token:      <redacted>")
        self.assertEqual(config.redact("token FAKETOKEN0123456789 already used on a prior"),
                         "token <redacted> already used on a prior")
        self.assertEqual(config.redact("adopt token changed (FAKETOKEN012 -> Ab12Cd34Ef56Gh)"),
                         "adopt token changed (<redacted>)")
        self.assertEqual(config.redact("reconnecting tokenless (fingerprint pinned)"),
                         "reconnecting tokenless (fingerprint pinned)")

    def test_new_password_and_query_credentials(self):
        for line in (
            "payload={'newPassword': 'P@ssw0rd!'}",
            "rtsp://10.0.0.9/live?username=admin&password=S3cret",
            "rtsp://admin:p@ssw0rd@10.0.0.9/live",
        ):
            out = config.redact(line)
            self.assertNotIn("P@ssw0rd", out)
            self.assertNotIn("S3cret", out)
            self.assertNotIn("p@ssw0rd", out)

    def test_leaves_ordinary_text(self):
        for line in ("connecting to wss://192.168.1.2:7442/ with headers={'X-Ident': 'fc'}",
                     "starting merged RTSP pull for deviceID=D: rtsp://10.0.0.1:7447/abc"):
            self.assertEqual(config.redact(line), line)

    def test_filter_rewrites_record(self):
        import logging
        rec = logging.LogRecord("t", logging.INFO, __file__, 1, "url=%s n=%d",
                                ("rtsp://u:p@cam/1", 3), None)
        self.assertTrue(config.RedactFilter().filter(rec))
        self.assertEqual(rec.getMessage(), "url=rtsp://<redacted>@cam/1 n=3")



class TestAdvertisedIp(unittest.TestCase):
    def test_reported_ip_can_differ_from_the_bound_one(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"AIPORT_ADVERTISE_IP": "192.168.1.39"}):
            self.assertEqual(config.advertised_ip("192.168.215.2"), "192.168.1.39")
        with mock.patch.dict(os.environ, {"AIPORT_ADVERTISE_IP": ""}):
            self.assertEqual(config.advertised_ip("192.168.215.2"), "192.168.215.2")

if __name__ == "__main__":
    unittest.main()
