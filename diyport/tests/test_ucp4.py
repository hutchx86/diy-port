# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for ucp4_client.py's wire framing (pack/unpack records)."""
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import ucp4_client  # noqa: E402
import config  # noqa: E402


class TestPackRecord(unittest.TestCase):
    def test_header_shape(self):
        payload = {"a": 1, "b": [1, 2, 3]}
        rec = ucp4_client.pack_record(3, payload)
        rtype, version, reserved, length = struct.unpack(">BB4sH", rec[:8])
        self.assertEqual(rtype, 3)
        self.assertEqual(version, 1)
        self.assertEqual(reserved, b"\x00\x00\x00\x00")
        body = rec[8:]
        self.assertEqual(length, len(body))
        self.assertEqual(json.loads(body), payload)

    def test_length_is_body_only(self):
        rec = ucp4_client.pack_record(9, {})
        self.assertEqual(struct.unpack(">H", rec[6:8])[0], 2)  # len("{}")

    def test_pack_message_is_head_then_body(self):
        data = ucp4_client.pack_message({"h": 1}, {"b": 2})
        recs = ucp4_client.unpack_all_records(data)
        self.assertEqual([r[0] for r in recs], [1, 2])
        self.assertEqual(recs[0][2], {"h": 1})
        self.assertEqual(recs[1][2], {"b": 2})

    def test_header_uses_u32_length(self):
        # A body larger than 65535 must round-trip (a u16 length would truncate).
        payload = {"blob": "x" * 70000}
        rec = ucp4_client.pack_record(2, payload)
        self.assertEqual(struct.unpack(">I", rec[4:8])[0], len(rec) - 8)
        self.assertEqual(ucp4_client.unpack_all_records(rec)[0][2], payload)

    def test_raw_record_format3(self):
        jpeg = b"\xff\xd8" + b"\x00" * 100000 + b"\xff\xd9"
        rec = ucp4_client.pack_raw_record(2, jpeg)
        rtype, rec_format, _compressed, _reserved, length = struct.unpack(">BBBBI", rec[:8])
        self.assertEqual((rtype, rec_format, length), (2, 3, len(jpeg)))
        self.assertEqual(rec[8:], jpeg)

    def test_pack_raw_message_head_json_body_raw(self):
        data = ucp4_client.pack_raw_message({"type": "response", "id": "n"}, b"\xff\xd8\xff\xd9")
        recs = ucp4_client.unpack_all_records(data)
        self.assertEqual(recs[0][2], {"type": "response", "id": "n"})
        self.assertEqual(recs[1][1], 3)  # raw format marker
        self.assertEqual(recs[1][2], {"_raw_bytes": b"\xff\xd8\xff\xd9"})


class TestUnpackAllRecords(unittest.TestCase):
    def test_roundtrip_multiple(self):
        data = (ucp4_client.pack_record(1, {"x": "y"})
                + ucp4_client.pack_record(2, {"n": 42})
                + ucp4_client.pack_record(3, {"l": [1, 2]}))
        recs = ucp4_client.unpack_all_records(data)
        self.assertEqual([r[0] for r in recs], [1, 2, 3])
        self.assertEqual([r[2] for r in recs], [{"x": "y"}, {"n": 42}, {"l": [1, 2]}])
        self.assertTrue(all(r[1] == 1 for r in recs))

    def test_empty(self):
        self.assertEqual(ucp4_client.unpack_all_records(b""), [])

    def test_non_json_payload_is_raw(self):
        body = b"not json"
        data = struct.pack(">BB4sH", 7, 1, b"\x00\x00\x00\x00", len(body)) + body
        recs = ucp4_client.unpack_all_records(data)
        self.assertEqual(recs, [(7, 1, {"_raw": "not json"})])

    def test_trailing_partial_header_ignored(self):
        data = ucp4_client.pack_record(1, {"a": 1}) + b"\x01\x02\x03"
        recs = ucp4_client.unpack_all_records(data)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0][2], {"a": 1})


class TestGetSnapshot(unittest.TestCase):
    """handle_action('getSnapshot') feeds Protect's paired-camera overview /
    timeline thumbnail fetch, which targets the AI Port over UCP4."""

    def setUp(self):
        self._orig = ucp4_client.STREAM_DIR
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        ucp4_client.STREAM_DIR = self.d
        self.addCleanup(lambda: setattr(ucp4_client, "STREAM_DIR", self._orig))

    def _write(self, name, data, age=0.0):
        path = os.path.join(self.d, name)
        with open(path, "wb") as f:
            f.write(data)
        if age:
            os.utime(path, (time.time() - age, time.time() - age))

    def test_returns_newest_valid_jpeg_as_raw(self):
        self._write("old.jpg", b"\xff\xd8old\xff\xd9", age=10)
        self._write("new.jpg", b"\xff\xd8new\xff\xd9")
        reply = ucp4_client.handle_action({"action": "getSnapshot"}, {}, {})
        self.assertIsInstance(reply, ucp4_client.RawReply)
        self.assertEqual(reply.data, b"\xff\xd8new\xff\xd9")

    def test_skips_torn_frame(self):
        self._write("good.jpg", b"\xff\xd8good\xff\xd9", age=10)
        self._write("torn.jpg", b"\xff\xd8half")  # no EOI
        reply = ucp4_client.handle_action({"action": "getSnapshot"}, {}, {})
        self.assertEqual(reply.data, b"\xff\xd8good\xff\xd9")

    def test_no_frame_reports_error(self):
        reply = ucp4_client.handle_action({"action": "getSnapshot"}, {}, {})
        self.assertIsInstance(reply, ucp4_client.ErrorReply)
        self.assertNotEqual(reply.error_code, 0)


class TestPinnedGetInfo(unittest.TestCase):
    """ucp4 getInfo reports a pinned (simulated-update) version over the configured one."""

    DEVICE = {"mac_nosep": "fcecda000001", "type": "UVC AI Port", "version": "5.1.12.67",
              "guid": "5c2659d4-48f3-16c4-ed83-2fefbad37066", "device_id": "d",
              "ip": "10.0.0.1"}

    def setUp(self):
        self._orig = ucp4_client.STATE_DIR
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        ucp4_client.STATE_DIR = self.d
        self.addCleanup(lambda: setattr(ucp4_client, "STATE_DIR", self._orig))

    def test_configured_when_unpinned(self):
        reply = ucp4_client.handle_action({"action": "getInfo"}, {}, dict(self.DEVICE))
        self.assertEqual(reply["fwVersion"], "5.1.12.67")

    def test_pinned_wins(self):
        config.atomic_write_json(os.path.join(self.d, "fw_version.json"),
                                 {"version": "5.9.9.9"})
        reply = ucp4_client.handle_action({"action": "getInfo"}, {}, dict(self.DEVICE))
        self.assertEqual(reply["fwVersion"], "5.9.9.9")
        self.assertEqual(reply["version"], "5.9.9.9")


if __name__ == "__main__":
    unittest.main()
