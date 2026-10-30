# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Unit tests for discovery.py's discovery response builder (no sockets)."""
import os
import socket
import struct
import sys
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import discovery  # noqa: E402


def parse_tlvs(payload):
    out = {}
    off = 0
    while off < len(payload):
        t = payload[off]
        length = struct.unpack(">H", payload[off + 1:off + 3])[0]
        out[t] = payload[off + 3:off + 3 + length]
        off += 3 + length
    return out


def build(adopted=False, console_id=b""):
    mac = bytes.fromhex("fcecda010203")
    ip = socket.inet_aton("192.0.2.110")
    with mock.patch.object(discovery, "is_adopted", return_value=adopted), \
         mock.patch.object(discovery, "get_console_id_bytes", return_value=console_id):
        return discovery.build_response(
            1, 0, mac, ip, "diyport", "UVC.S2L.v5.1.12.67", "UVC AI Port",
            0xA5F1, 1234, "fe6488e7-7042-5bcb-ab86-6f0ad1a5baed")


class TestTlv(unittest.TestCase):
    def test_tlv_encoding(self):
        self.assertEqual(discovery.tlv(0x0C, b"abc"), b"\x0c\x00\x03abc")
        self.assertEqual(discovery.tlv(0x10, struct.pack("<H", 0xA5F1)),
                         b"\x10\x00\x02\xf1\xa5")
        self.assertEqual(discovery.tlv(0x3F, b"\x01"), b"\x3f\x00\x01\x01")

    def test_empty_value(self):
        self.assertEqual(discovery.tlv(0x05, b""), b"\x05\x00\x00")


class TestBuildResponse(unittest.TestCase):
    def test_header_and_fields(self):
        pkt = build(adopted=False)
        version, command, data_len = struct.unpack(">BBH", pkt[:4])
        self.assertEqual((version, command), (1, 0))
        payload = pkt[4:]
        self.assertEqual(data_len, len(payload))

        t = parse_tlvs(payload)
        mac = bytes.fromhex("fcecda010203")
        self.assertEqual(t[discovery.TLV_HW_ADDR], mac)
        self.assertEqual(t[discovery.TLV_IP_INFO], mac + socket.inet_aton("192.0.2.110"))
        self.assertEqual(struct.unpack(">I", t[discovery.TLV_UPTIME])[0], 1234)
        self.assertEqual(t[discovery.TLV_HOSTNAME], b"diyport")
        self.assertEqual(t[discovery.TLV_PLATFORM], b"UVC AI Port")
        self.assertEqual(t[discovery.TLV_FW_VERSION], b"UVC.S2L.v5.1.12.67")
        self.assertEqual(struct.unpack("<H", t[discovery.TLV_SYSID])[0], 0xA5F1)
        self.assertEqual(t[discovery.TLV_DEVICE_ID], b"fe6488e7-7042-5bcb-ab86-6f0ad1a5baed")
        self.assertEqual(t[discovery.TLV_SUPPORT_UCP4], b"\x01")
        self.assertEqual(t[discovery.TLV_DEFAULT_CREDENTIALS], b"\x03")
        # never send the forbidden tag IDs
        self.assertNotIn(0x05, t)
        self.assertNotIn(0x14, t)

    def test_is_managed_flag(self):
        self.assertEqual(struct.unpack(">I", parse_tlvs(build(adopted=False)[4:])[discovery.TLV_IS_MANAGED])[0], 1)
        self.assertEqual(struct.unpack(">I", parse_tlvs(build(adopted=True)[4:])[discovery.TLV_IS_MANAGED])[0], 0)

    def test_controller_id_only_when_present(self):
        self.assertNotIn(discovery.TLV_CONTROLLER_ID, parse_tlvs(build(console_id=b"")[4:]))
        cid = bytes(range(16))
        self.assertEqual(parse_tlvs(build(console_id=cid)[4:])[discovery.TLV_CONTROLLER_ID], cid)


if __name__ == "__main__":
    unittest.main()
