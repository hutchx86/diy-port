#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Outbound mTLS WebSocket ("ucp4") client for the AI Port emulator.

8-byte header + JSON envelope; AI Port uses the real camera ubnt_avclient
codebase, so AI-Key-specific handlers don't apply. Unknown actions are acked
with `{}` and logged so the controller's errors.log can reveal real shapes.
"""
import argparse
import json
import logging
import os
import ssl
import struct
import sys
import threading
import time
import uuid

from websockets.sync.client import connect

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

log = logging.getLogger("aiport-ucp4-client")

_PROCESS_START_MS = int(time.time() * 1000)

HERE = os.path.dirname(os.path.abspath(__file__))
ADOPT_STATE_FILE = os.path.join(HERE, "adopt_state.json")
CERT_PATH = os.path.join(HERE, "server.crt")
KEY_PATH = os.path.join(HERE, "server.key")
# RAM-only snapshot dir avclient.py's ffmpeg writes; same dir http_api.py serves.
STREAM_DIR = config.stream_dir()
# --state-dir (or None for the shared piport/ path); the firmware pin lives here.
STATE_DIR = None

# UCP4 record header (8 bytes): type u8, format u8, compressed u8, reserved u8,
# length u32 BE (the controller reads length at offset 4, matching this layout).
RECORD_TYPE_HEAD = 1
RECORD_TYPE_BODY = 2
RECORD_FORMAT_JSON = 1
# format=3 means the body is a raw Buffer, not JSON. Needed to hand the
# controller a JPEG byte-for-byte (it checks `Buffer.isBuffer()`).
RECORD_FORMAT_RAW = 3


def pack_record(record_type: int, payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    header = struct.pack(">BBBBI", record_type, RECORD_FORMAT_JSON, 0, 0, len(body))
    return header + body


def pack_raw_record(record_type: int, payload: bytes) -> bytes:
    header = struct.pack(">BBBBI", record_type, RECORD_FORMAT_RAW, 0, 0, len(payload))
    return header + payload


def pack_message(head: dict, body: dict) -> bytes:
    return pack_record(RECORD_TYPE_HEAD, head) + pack_record(RECORD_TYPE_BODY, body)


def pack_raw_message(head: dict, body: bytes) -> bytes:
    """Head as JSON, body as raw bytes (format=3) so the controller resolves a
    Buffer rather than a parsed object -- how a real device returns a JPEG."""
    return pack_record(RECORD_TYPE_HEAD, head) + pack_raw_record(RECORD_TYPE_BODY, body)


def unpack_all_records(data: bytes):
    records = []
    off = 0
    while off < len(data):
        if len(data) - off < 8:
            log.warning("trailing %d bytes, not enough for a record header", len(data) - off)
            break
        rtype, rec_format, _compressed, _reserved, length = struct.unpack(
            ">BBBBI", data[off:off + 8])
        off += 8
        payload = data[off:off + length]
        off += length
        if rec_format == RECORD_FORMAT_RAW:
            obj = {"_raw_bytes": payload}
        else:
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                obj = {"_raw": payload.decode(errors="replace")}
        records.append((rtype, rec_format, obj))
    return records


class RawReply:
    """Marker: reply body is raw bytes (format=3), not JSON."""

    def __init__(self, data: bytes):
        self.data = data


class ErrorReply:
    """Marker: reply with a non-zero errorCode so the controller falls back."""

    def __init__(self, error_code: int, error: str = ""):
        self.error_code = error_code
        self.error = error


def wait_for_adopt_state(fallback_host_port, poll_interval=1.0, fallback_after=10.0):
    log.info("waiting for %s to appear (written by http_api.py on a successful adopt POST)...",
              ADOPT_STATE_FILE)
    waited = 0.0
    while True:
        if os.path.exists(ADOPT_STATE_FILE):
            try:
                with open(ADOPT_STATE_FILE) as f:
                    state = json.load(f)
                if state.get("hosts"):
                    return state
            except (OSError, json.JSONDecodeError):
                pass
        if fallback_host_port and waited >= fallback_after:
            log.info("no adopt POST seen after %.0fs -- falling back to tokenless reconnect "
                      "to %s (device may already be adopted server-side)",
                      waited, fallback_host_port)
            return {"hosts": [fallback_host_port], "token": None}
        time.sleep(poll_interval)
        waited += poll_interval


def build_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(CERT_PATH, KEY_PATH)
    return ctx


def _read_snapshot_jpeg(timeout=2.0):
    """Return the freshest complete JPEG any paired camera wrote to STREAM_DIR,
    or None. The controller's snapshot request carries no camera id (real AI
    Ports serve the same way), so the newest frame is the best available match;
    a torn/mid-write file is retried briefly and skipped."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            names = [n for n in os.listdir(STREAM_DIR) if n.endswith(".jpg")]
        except OSError:
            names = []
        names.sort(key=lambda n: os.path.getmtime(os.path.join(STREAM_DIR, n)),
                   reverse=True)
        for name in names[:3]:
            try:
                with open(os.path.join(STREAM_DIR, name), "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if len(data) > 4 and data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
                return data
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


def handle_action(head, body, device_info):
    action = head.get("action")
    log.info("<<< controller action=%r head=%s body=%s", action, head, body)

    if action == "getInfo":
        # Report a pinned (simulated-update) version if present; an empty fwVersion
        # disables firmware-gated features, and a non-null hwrev beats an empty column.
        fw = config.effective_fw_version(device_info["version"], STATE_DIR)
        now_ms = int(time.time() * 1000)
        return {
            "mac": device_info["mac_nosep"],
            "type": device_info["type"],
            "fwVersion": fw,
            "version": fw,
            "hwrev": 1,
            # Read live so ResetToDefaults can't leave this claiming still-adopted.
            "adopted": _read_current_token() is not None,
            "guid": device_info["guid"],
            "deviceId": device_info["device_id"],
            "ip": device_info["ip"],
            # The controller's host, not ours (used to build upload URLs).
            "connectionHost": device_info.get("console_host") or device_info["ip"],
            "connectionSecurePort": 443,
            "uptime": (now_ms - _PROCESS_START_MS) // 1000,
            "upSince": _PROCESS_START_MS,
            "protocolVersion": 1,
            "poeType": "unknown",
        }
    if action == "networkStatus":
        return {"ip": device_info["ip"], "connected": True}
    if action == "changeUserPassword":
        log.info("changeUserPassword requested (not persisted in this emulator)")
        return {}

    if action == "getSnapshot":
        # Reply with the JPEG as a raw (format=3) body; `{}` makes the controller
        # fall back to REST and, on failure, leaves the event without a thumbnail.
        data = _read_snapshot_jpeg()
        if data:
            log.info("getSnapshot: replying with %d-byte JPEG from %s", len(data), STREAM_DIR)
            return RawReply(data)
        log.warning("getSnapshot requested but no captured frame is available in %s "
                    "-- reporting an error so the controller falls back to REST",
                    STREAM_DIR)
        return ErrorReply(1, "no snapshot available")

    # Ack `{}` so the controller's RPC doesn't hang; log it for shape discovery.
    return {}


def _read_current_token():
    if not os.path.exists(ADOPT_STATE_FILE):
        return None
    try:
        with open(ADOPT_STATE_FILE) as f:
            return json.load(f).get("token")
    except (OSError, json.JSONDecodeError):
        return None


def _watch_for_reconnect(ws, this_connection_token, stop_event, poll_interval=2.0):
    while not stop_event.is_set():
        time.sleep(poll_interval)
        current = _read_current_token()
        # Any token change (including clearing) makes X-Adopted stale, so tear down.
        if current != this_connection_token:
            log.info("adopt token changed (%s -> %s) -- closing this connection "
                      "to force a fresh one", this_connection_token, current)
            try:
                ws.close()
            except Exception:
                pass
            return


def run(host: str, port: int, token: str, send_token_header: bool, device_info: dict,
        adopted: bool = True):
    device_info = {**device_info, "console_host": host}
    url = f"wss://{host}:{port}/"
    headers = {
        "X-Ident": device_info["mac_nosep"],
        "X-Mode": "0",
        "X-Type": device_info["type"],
        "X-Adopted": "true" if adopted else "false",
        "X-Ip": device_info["ip"],
        "X-Device-Id": device_info["device_id"],
        "X-Version": config.effective_fw_version(device_info["version"], STATE_DIR),
        "X-Guid": device_info["guid"],
    }
    if send_token_header:
        headers["X-Token"] = token

    log.info("connecting to %s with headers=%s", url, headers)
    ctx = build_ssl_context()

    with connect(url, subprotocols=["ucp4"], additional_headers=headers,
                 ssl=ctx, open_timeout=15) as ws:
        log.info("WSS CONNECTED (subprotocol=%s)", ws.subprotocol)

        stop_watch = threading.Event()
        watcher = threading.Thread(target=_watch_for_reconnect, args=(ws, token, stop_watch),
                                    daemon=True)
        watcher.start()

        req_id = str(uuid.uuid4())
        msg = pack_message(
            {"timestamp": int(time.time() * 1000), "type": "request",
             "action": "getConsoleInfo", "id": req_id},
            {},
        )
        ws.send(msg)
        log.info(">>> sent getConsoleInfo request id=%s", req_id)

        try:
            _consume(ws, device_info)
        finally:
            stop_watch.set()


def _consume(ws, device_info):
    for raw in ws:
        if isinstance(raw, str):
            log.warning("unexpected TEXT frame (protocol says binary-only): %r", raw[:200])
            continue
        records = unpack_all_records(raw)
        if len(records) < 2:
            log.warning("frame with <2 records: %s", records)
            continue
        head = records[0][2]
        body = records[1][2]

        if head.get("type") == "response":
            log.info("<<< response to our request id=%s errorCode=%s body=%s",
                      head.get("id"), head.get("errorCode"), body)
            continue

        reply_body = handle_action(head, body, device_info)
        if head.get("id"):
            error_code, error, binary = 0, "", None
            if isinstance(reply_body, RawReply):
                binary, reply_body = reply_body.data, {}
            elif isinstance(reply_body, ErrorReply):
                error_code, error = reply_body.error_code, reply_body.error
            reply_head = {"timestamp": int(time.time() * 1000), "type": "response",
                          "action": head.get("action"), "id": head["id"],
                          "errorCode": error_code, "error": error}
            frame = (pack_raw_message(reply_head, binary) if binary is not None
                     else pack_message(reply_head, reply_body))
            ws.send(frame)
            log.info(">>> replied to action=%s id=%s errorCode=%s body=%s",
                      head.get("action"), head["id"], error_code,
                      f"<{len(binary)} raw bytes>" if binary is not None else reply_body)


def main():
    cfg = config.load_config_and_logging(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__)
    config.add_common_flags(ap)
    ap.add_argument("--iface", default=None,
                     help="interface to derive identity from (never bound); defaults to cfg "
                          "[network] iface, else the default-route interface (auto-detected)")
    ap.add_argument("--bind-ip", default="0.0.0.0",
                     help="local IP to report as our own (X-Ip header etc); does not "
                          "actually bind a socket to it since this is an outbound client")
    ap.add_argument("--mac", default=None)
    ap.add_argument("--type", default=cfg["platform"],
                     help="catalog model string (see discovery.py's --platform help)")
    ap.add_argument("--version", default=cfg["fw_version"])
    ap.add_argument("--device-id", default=cfg["device_id"])
    ap.add_argument("--guid", default=cfg["guid"])
    ap.add_argument("--fallback-host-port", default=None,
                     help="host:port to reconnect to (tokenless) if no fresh adopt POST "
                          "shows up within --fallback-after seconds")
    ap.add_argument("--fallback-after", type=float, default=10.0)
    ap.add_argument("--state-dir", default=None,
                     help="where adopt_state.json/server.crt/server.key live -- defaults "
                          "to the shared piport/ path (single-instance, unchanged "
                          "behavior); set this to isolate a second/third AI Port instance "
                          "running on the same box (see instance_manager.py)")
    args = ap.parse_args()
    args.iface = config.resolve_iface(args.iface, cfg)

    if args.state_dir:
        global ADOPT_STATE_FILE, CERT_PATH, KEY_PATH, STREAM_DIR, STATE_DIR
        os.makedirs(args.state_dir, exist_ok=True)
        ADOPT_STATE_FILE = os.path.join(args.state_dir, "adopt_state.json")
        CERT_PATH = os.path.join(args.state_dir, "server.crt")
        KEY_PATH = os.path.join(args.state_dir, "server.key")
        # Same RAM-only dir avclient.py writes and http_api.py serves.
        STREAM_DIR = config.stream_dir(args.state_dir)
        STATE_DIR = args.state_dir

    mac_nosep = config.resolve_mac(args.iface, args.mac, cfg).lower()
    ip = config.advertised_ip(config.resolve_ip(args.iface, cfg, args.bind_ip))

    device_info = {
        "mac_nosep": mac_nosep.replace(":", ""),
        "type": args.type,
        "version": args.version,
        "device_id": args.device_id,
        "guid": args.guid,
        "ip": ip,
    }

    # Tokens are single-use: send X-Token only the first time a value is seen.
    used_tokens = set()

    while True:
        state = wait_for_adopt_state(args.fallback_host_port, fallback_after=args.fallback_after)
        hosts = state.get("hosts") or []
        token = state.get("token")
        # The tokenless fallback means never adopted as this identity: report
        # X-Adopted: false.
        adopted = token is not None
        if not hosts:
            log.warning("adopt state had no hosts, retrying in 5s")
            time.sleep(5)
            continue
        host_port = hosts[0]
        try:
            if ":" in host_port:
                host, port_s = host_port.rsplit(":", 1)
                port = int(port_s)
            else:
                host, port = host_port, 7442
        except (TypeError, ValueError):
            log.warning("malformed adopt state host %r; retrying in 5s", host_port)
            time.sleep(5)
            continue

        send_token = token is not None and token not in used_tokens
        if token is not None:
            used_tokens.add(token)
        if token is not None and not send_token:
            log.info("token %s already used on a prior connection -- reconnecting "
                      "tokenless (fingerprint should already be pinned)", token)
        try:
            run(host, port, token, send_token, device_info, adopted=adopted)
            log.warning("ucp4 connection closed cleanly by peer; reconnecting in 3s")
        except Exception as e:
            log.warning("connection ended: %r -- reconnecting in 3s", e)
        time.sleep(3)


if __name__ == "__main__":
    main()
