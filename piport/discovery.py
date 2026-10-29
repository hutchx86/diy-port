#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""UBNT L2 discovery responder (UDP/10001) for the AI Port emulator.

Classic UBNT discovery wire format. Sysid 0xa5f1 must map to catalog model
"UVC AI Port" for adoption; the discovery platform TLV (0x0C) advertises the
SKU "UP-AI-Port" ([identity] discovery_platform). The controller scans multicast
233.89.188.1:10001 (not broadcast), so bind 0.0.0.0, scope the socket to the
interface where the OS supports SO_BINDTODEVICE, and join the group (which also
programs the NIC filter).
"""
import argparse
import json
import logging
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

HERE = os.path.dirname(os.path.abspath(__file__))
ADOPT_STATE_FILE = os.path.join(HERE, "adopt_state.json")
# --state-dir (or None for the shared piport/ path); the firmware pin lives here.
STATE_DIR = None

log = logging.getLogger("aiport-discovery")

DISCOVERY_PORT = 10001
# The controller's adopt-candidate scan destination (not broadcast).
MCAST_GROUP = "233.89.188.1"

TLV_HW_ADDR = 0x01
TLV_IP_INFO = 0x02
TLV_FW_VERSION = 0x03
TLV_UPTIME = 0x0A
TLV_HOSTNAME = 0x0B
TLV_PLATFORM = 0x0C
TLV_SYSID = 0x10
TLV_IS_MANAGED = 0x17
TLV_DEVICE_ID = 0x20
# Real cameras omit 0x05/0x14 (dups of HWADDR/PLATFORM) and send a 4-byte
# IS_MANAGED; CONTROLLER_ID is 16 raw bytes of the console anonymousDeviceId.
TLV_CONTROLLER_ID = 0x26
# Real cameras send a DEFAULT_CREDENTIALS byte (value 3).
TLV_DEFAULT_CREDENTIALS = 0x2C
# tag 63, single boolean byte; gates needUpdateBeforeAdoption.
TLV_SUPPORT_UCP4 = 0x3F
# Do NOT send TLV_GUID (0x2B) from a device beacon; it is a console identity field.


def tlv(t: int, value: bytes) -> bytes:
    return struct.pack(">BH", t, len(value)) + value


def _read_adopt_state() -> dict:
    try:
        with open(ADOPT_STATE_FILE) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def is_adopted() -> bool:
    state = _read_adopt_state()
    return bool(state.get("token")) or bool(state.get("hosts"))


def get_console_id_bytes() -> bytes:
    """16 raw bytes of the adopted console's anonymousDeviceId UUID, or None.
    Sent as TLV_CONTROLLER_ID."""
    console_id = _read_adopt_state().get("consoleId")
    if not console_id:
        return None
    try:
        return bytes.fromhex(console_id.replace("-", ""))
    except ValueError:
        return None


def build_response(version: int, command: int, mac: bytes, ip: bytes, hostname: str,
                    fw_version: str, platform: str, sysid: int, uptime_s: int,
                    device_id: str) -> bytes:
    payload = b""
    payload += tlv(TLV_IP_INFO, mac + ip)
    payload += tlv(TLV_HW_ADDR, mac)
    payload += tlv(TLV_UPTIME, struct.pack(">I", uptime_s))
    payload += tlv(TLV_HOSTNAME, hostname.encode())
    payload += tlv(TLV_PLATFORM, platform.encode())
    payload += tlv(TLV_IS_MANAGED, struct.pack(">I", 0 if is_adopted() else 1))
    payload += tlv(TLV_FW_VERSION, fw_version.encode())
    payload += tlv(TLV_SYSID, struct.pack("<H", sysid))
    payload += tlv(TLV_DEVICE_ID, device_id.encode())
    console_id_bytes = get_console_id_bytes()
    if console_id_bytes:
        payload += tlv(TLV_CONTROLLER_ID, console_id_bytes)
    payload += tlv(TLV_SUPPORT_UCP4, bytes([0x01]))
    payload += tlv(TLV_DEFAULT_CREDENTIALS, bytes([3]))

    header = struct.pack(">BBH", version, command, len(payload))
    return header + payload


def main():
    cfg = config.load_config_and_logging(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__)
    config.add_common_flags(ap)
    ap.add_argument("--iface", default=None,
                     help="interface to bind/derive identity from; defaults to cfg [network] iface, "
                          "else the default-route interface (auto-detected)")
    ap.add_argument("--bind-ip", default="0.0.0.0",
                     help="bind the UDP socket to this specific IP instead of 0.0.0.0, "
                          "so this can coexist with another discovery responder "
                          "(e.g. aikey-emu's) on the same host")
    ap.add_argument("--mac", default=None, help="override MAC (hex, with or without colons)")
    ap.add_argument("--hostname", default=cfg["hostname"])
    ap.add_argument("--platform", default=cfg.get("discovery_platform") or cfg["platform"],
                     help="discovery platform TLV 0x0C. Must be the SKU string Network's "
                          "fingerprint catalogue knows ('UP-AI-Port'), not the canonical "
                          "Protect type; defaults to [identity] discovery_platform, then "
                          "[identity] platform")
    ap.add_argument("--sysid", type=lambda s: int(s, 0), default=config.sysid_int(cfg),
                     help="CONFIRMED from the real controller's service.js AI_PORT_SYSIDS map")
    ap.add_argument("--fw-version", default=cfg["discovery_fw_version"])
    ap.add_argument("--device-id", default=cfg["device_id"])
    ap.add_argument("--state-dir", default=None,
                     help="where adopt_state.json lives -- defaults to the shared "
                          "piport/ path (single-instance, unchanged behavior); "
                          "set this to isolate a second/third AI Port instance running "
                          "on the same box (see instance_manager.py)")
    args = ap.parse_args()
    args.iface = config.resolve_iface(args.iface, cfg)

    if args.state_dir:
        global ADOPT_STATE_FILE, STATE_DIR
        os.makedirs(args.state_dir, exist_ok=True)
        ADOPT_STATE_FILE = os.path.join(args.state_dir, "adopt_state.json")
        STATE_DIR = args.state_dir

    mac = bytes.fromhex(config.resolve_mac(args.iface, args.mac, cfg))
    ip = config.advertised_ip(config.resolve_ip(args.iface, cfg, args.bind_ip))
    ip_bytes = socket.inet_aton(ip)
    start = time.time()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # Scope to this interface so one instance can't answer another's probes.
    # SO_BINDTODEVICE is Linux-only; macOS/BSD simply leave the socket unscoped.
    bind_device = getattr(socket, "SO_BINDTODEVICE", None)
    if bind_device is None:
        log.info("SO_BINDTODEVICE not available on this platform; discovery socket "
                 "is not interface-scoped")
    else:
        try:
            sock.setsockopt(socket.SOL_SOCKET, bind_device, args.iface.encode() + b"\0")
        except OSError as e:
            log.warning("SO_BINDTODEVICE(%s) failed: %s -- this instance may see/answer "
                         "discovery probes meant for other instances on the same box",
                         args.iface, e)
    sock.bind(("0.0.0.0", DISCOVERY_PORT))

    # Join scoped to this interface; required to receive multicast and to program the NIC filter.
    try:
        if_index = socket.if_nametoindex(args.iface)
        try:
            # Linux: struct ip_mreqn (group, 0.0.0.0, ifindex).
            mreq = socket.inet_aton(MCAST_GROUP) + socket.inet_aton("0.0.0.0") + \
                struct.pack("@i", if_index)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError:
            # BSD/macOS: struct ip_mreq carries the interface address, not an index.
            mreq = socket.inet_aton(MCAST_GROUP) + socket.inet_aton(ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        log.info("Joined multicast group %s on %s (ifindex %d)",
                  MCAST_GROUP, args.iface, if_index)
    except OSError as e:
        log.warning("failed to join multicast group %s on %s: %s -- this instance "
                     "will only be discoverable via unicast re-probes of an already-"
                     "known IP, not the initial adopt-candidate scan",
                     MCAST_GROUP, args.iface, e)

    log.info("Listening for UBNT discovery probes on %s (bound to %s, mac=%s platform=%r "
              "sysid=0x%04x)", args.iface, args.iface, mac.hex(":"), args.platform, args.sysid)

    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except OSError as e:
            log.warning("recv error: %s", e)
            continue

        log.debug("Discovery probe from %s: %s", addr, data.hex())

        if len(data) < 4:
            log.warning("probe too short, ignoring")
            continue
        req_version, req_command = data[0], data[1]

        if (req_version, req_command) == (1, 0):
            resp_version, resp_command = 1, 0
        elif (req_version, req_command) == (2, 8):
            resp_version, resp_command = 2, 6
        else:
            log.warning("unrecognized request version/command (%d/%d), replying v1 anyway",
                        req_version, req_command)
            resp_version, resp_command = 1, 0

        uptime_s = int(time.time() - start)
        resp = build_response(resp_version, resp_command, mac, ip_bytes,
                               config.device_name(STATE_DIR, cfg.get("name")),
                               config.effective_fw_version(args.fw_version, STATE_DIR),
                               args.platform, args.sysid, uptime_s,
                               args.device_id)
        try:
            sock.sendto(resp, addr)
            log.debug("Sent v%d/cmd%d discovery response to %s (%d bytes)",
                      resp_version, resp_command, addr, len(resp))
        except OSError as e:
            log.warning("send error to %s: %s", addr, e)


if __name__ == "__main__":
    main()
