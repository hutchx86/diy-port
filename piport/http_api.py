#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""HTTPS control API on :443, standing in for AI Port's local ubnt_ctlserver:
GET/POST /api/info, POST /api/1.2/manage (adopt), plus login/status/snapshot.
The controller does not validate the server cert.
"""
import argparse
import http.server
import json
import logging
import os
import ssl
import subprocess
import sys
import time
from urllib.parse import urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

log = logging.getLogger("aiport-http-api")

HERE = os.path.dirname(os.path.abspath(__file__))
CERT_PATH = os.path.join(HERE, "server.crt")
KEY_PATH = os.path.join(HERE, "server.key")
ADOPT_STATE_FILE = os.path.join(HERE, "adopt_state.json")
# Refuse oversized/absent request bodies instead of allocating whatever a peer claims.
MAX_BODY_BYTES = 1 << 20
REQUEST_TIMEOUT_S = 10
# RAM-only dir shared with avclient.py (writes snapshots); served from /api/1.2/snapshot.
STREAM_DIR = config.stream_dir()
# --state-dir (or None for the shared piport/ path); the firmware pin lives here.
STATE_DIR = None
_STARTED_MONOTONIC = time.monotonic()


def ensure_cert():
    if os.path.exists(CERT_PATH) and os.path.exists(KEY_PATH):
        return
    log.info("generating self-signed EC P-256 device cert (controller does not validate it)")
    subprocess.run([
        "openssl", "ecparam", "-out", KEY_PATH, "-name", "prime256v1", "-genkey", "-noout",
    ], check=True)
    subprocess.run([
        "openssl", "req", "-new", "-x509", "-sha256", "-key", KEY_PATH, "-out", CERT_PATH,
        "-days", "36500", "-subj", "/O=piport/CN=piport",
    ], check=True)


def _is_adopted() -> bool:
    # Read live (avclient.py clears it on ResetToDefaults) so isAdopted follows a real un-adopt.
    try:
        with open(ADOPT_STATE_FILE) as f:
            state = json.load(f)
        return bool(state.get("token")) or bool(state.get("hosts"))
    except (OSError, json.JSONDecodeError):
        return False


def _freshest_jpeg(stream_dir, timeout=2.0):
    """Newest complete JPEG in stream_dir, or None. ffmpeg rewrites each
    camera's `<deviceID>.jpg` in place, so a read can catch a torn frame; retry
    briefly and skip malformed files rather than serving a half-written image."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            names = [n for n in os.listdir(stream_dir) if n.endswith(".jpg")]
            names.sort(key=lambda n: os.path.getmtime(os.path.join(stream_dir, n)),
                       reverse=True)
        except OSError:
            names = []
        for name in names[:3]:
            try:
                with open(os.path.join(stream_dir, name), "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if len(data) > 4 and data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
                return name, data
        if time.monotonic() >= deadline:
            return None, None
        time.sleep(0.2)


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "piport-http/0.1"
    timeout = REQUEST_TIMEOUT_S

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        raw = self.headers.get("Content-Length", "0") or "0"
        try:
            length = int(raw)
        except ValueError:
            self.send_error(400, "bad Content-Length")
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self.send_error(413, "request body too large")
            return None
        if not length:
            return b""
        return self.rfile.read(length)

    def _route(self):
        # Strip any query string (`snapshot?source=2`) so routing matches.
        return urlsplit(self.path).path.rstrip("/")

    def _info_payload(self):
        cfg = self.server.aiport_config
        adopted = _is_adopted()
        return {
            "mac": cfg["mac"],
            "type": cfg["platform"],
            "sysid": f"0x{cfg['sysid']:04x}",
            "name": config.device_name(STATE_DIR, cfg.get("name")),
            "model": cfg["platform"],
            "firmwareVersion": config.effective_fw_version(cfg["fw_version"], STATE_DIR),
            "adopted": adopted,
            "isAdopted": adopted,
            "guid": cfg["guid"],
            "deviceId": cfg["device_id"],
            "anonymousDeviceId": cfg["device_id"],
            "ip": cfg["ip"],
            "uptime": int(time.monotonic() - _STARTED_MONOTONIC),
            # The controller's host, not ours (Protect builds upload URLs from it).
            "connectionHost": cfg.get("console_host") or cfg["ip"],
            # Real hwrev unconfirmed; placeholder matching ucp4 getInfo's hwrev.
            "hardwareRevision": 1,
        }

    def do_GET(self):
        log.info("GET %s from %s", self.path, self.client_address)
        route = self._route()
        if route in ("/api/info", "/info"):
            self._json(200, self._info_payload())
        elif route in ("/api/support", "/support"):
            self._json(200, {"statusCode": 200})
        elif route in ("/api/1.2/status",):
            self._serve_status()
        elif route in ("/api/1.2/snapshot", "/api/snapshot"):
            # REST snapshot pull (GET) is separate from POST; same handler.
            # `/api/snapshot` is the path the controller's ucp4 api-request uses.
            self._serve_snapshot()
        else:
            log.warning("unhandled GET path %s -- replying 200 anyway", self.path)
            self._json(200, {"statusCode": 200})

    def _serve_status(self):
        # Camera capability endpoint (getFeatureFlags/requestFeatureFlags); kept
        # honest to the detector (person/vehicle/animal + motion).
        cfg = self.server.aiport_config
        mac_colons = ":".join(cfg["mac"][i:i + 2] for i in range(0, 12, 2))
        self._json(200, {
            "fw": config.effective_fw_version(cfg["fw_version"], STATE_DIR),
            "board": {"hwaddr": mac_colons},
            "features": {
                "smartDetect": ["person", "vehicle", "animal", "lineCrossing", "liveviewTracking"],
                "motionDetect": ["enhanced"],
                "privacyMask": True,
                "privacyMasks": {"maxZones": 16, "rectangleOnly": False},
                "squareEventThumbnail": True,
                "mic": True,
                "speaker": True,
                "ledStatus": True,
            },
        })

    def do_POST(self):
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload = {"_raw": body.decode(errors="replace")}
        log.info("POST %s from %s body=%s", self.path, self.client_address,
                  json.dumps(payload))

        path = self._route()
        if path in ("/api/adopt", "/adopt"):
            mgmt_payload = payload
            ok = self._persist_adopt(mgmt_payload)
            self._json(200 if ok else 400,
                       {"statusCode": 200 if ok else 400})
        elif path in ("/api/1.2/manage",):
            # Real adopt endpoint (classic UVC management API; /api/adopt unused);
            # adopt fields nested under "mgmt".
            mgmt_payload = payload.get("mgmt", payload) if isinstance(payload, dict) else {}
            ok = self._persist_adopt(mgmt_payload)
            self._json(200 if ok else 400,
                       {"statusCode": 200 if ok else 400})
        elif path in ("/api/readopt", "/readopt"):
            self._json(200, {"statusCode": 200})
        elif path in ("/api/1.2/login",):
            # Controller logs in first and replays the Set-Cookie; no real auth.
            self.send_response(200)
            self.send_header("Set-Cookie", "AIROS_SESSIONID=piport-session; Path=/")
            body = json.dumps({"statusCode": 200}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path in ("/api/1.2/snapshot", "/api/snapshot"):
            # Serve the latest frame avclient.py's ffmpeg wrote to STREAM_DIR; 404 if none yet.
            self._serve_snapshot()
        else:
            log.warning("unhandled POST path %s -- replying 200 anyway", self.path)
            self._json(200, {"statusCode": 200})

    def _serve_snapshot(self):
        # The request carries no camera id, so serve the freshest complete frame;
        # a torn JPEG would be rejected and leave the event without a thumbnail.
        name, data = _freshest_jpeg(STREAM_DIR)
        if not name:
            log.warning("snapshot requested but no complete captured frame available in %s",
                        STREAM_DIR)
            self._json(404, {"statusCode": 404, "error": "no snapshot available"})
            return
        log.info("serving snapshot from %s (%d bytes)", os.path.join(STREAM_DIR, name),
                  len(data))
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _persist_adopt(self, mgmt_payload):
        if not isinstance(mgmt_payload, dict) or not (
                mgmt_payload.get("hosts") or mgmt_payload.get("token")):
            log.warning("ignoring adopt payload without hosts/token")
            return False
        self.server.aiport_config["last_adopt_payload"] = mgmt_payload
        log.info("=" * 70)
        log.info("ADOPT PAYLOAD RECEIVED")
        log.info("  hosts:      %s", mgmt_payload.get("hosts"))
        log.info("  token:      %s", mgmt_payload.get("token"))
        log.info("  protocol:   %s", mgmt_payload.get("protocol"))
        log.info("  consoleId:  %s", mgmt_payload.get("consoleId"))
        log.info("  consoleName:%s", mgmt_payload.get("consoleName"))
        log.info("  username:   %s", mgmt_payload.get("username"))
        log.info("=" * 70)
        config.atomic_write_json(ADOPT_STATE_FILE, mgmt_payload)
        return True

    def log_message(self, fmt, *args):
        pass


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    ssl_context = None

    def process_request_thread(self, request, client_address):
        """TLS-wrap in the worker thread, not in serve_forever's accept loop.

        Wrapping the listening socket made one stalled ClientHello block every
        subsequent connection (and Restart=on-failure could not help).
        """
        try:
            request.settimeout(REQUEST_TIMEOUT_S)
            tls = self.ssl_context.wrap_socket(request, server_side=True)
        except (OSError, ssl.SSLError) as exc:
            log.warning("TLS handshake from %s failed: %s", client_address, exc)
            self.shutdown_request(request)
            return
        try:
            self.finish_request(tls, client_address)
        except Exception:
            self.handle_error(tls, client_address)
        finally:
            self.shutdown_request(tls)


def main():
    cfg = config.load_config_and_logging(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__)
    config.add_common_flags(ap)
    ap.add_argument("--port", type=int, default=443,
                     help="AI Port's local API is on the standard camera HTTPS port 443 "
                          "(matches ubnt_ctlserver), confirmed from the controller's own "
                          "aiport.log ('connect ECONNREFUSED <device-ip>:443')")
    ap.add_argument("--iface", default=None,
                     help="interface to bind/derive identity from; defaults to cfg [network] iface, "
                          "else the default-route interface (auto-detected)")
    ap.add_argument("--bind-ip", default="0.0.0.0")
    ap.add_argument("--mac", default=None)
    ap.add_argument("--hostname", default=cfg["hostname"])
    ap.add_argument("--platform", default=cfg["platform"],
                     help="catalog model string (see discovery.py's --platform help)")
    ap.add_argument("--sysid", type=lambda s: int(s, 0), default=config.sysid_int(cfg))
    ap.add_argument("--fw-version", default=cfg["fw_version"])
    ap.add_argument("--device-id", default=cfg["device_id"])
    ap.add_argument("--guid", default=cfg["guid"])
    ap.add_argument("--state-dir", default=None,
                     help="where adopt_state.json/server.crt/server.key live -- defaults "
                          "to this script's own directory (single-instance, unchanged "
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
        # Both processes must derive the same RAM-only stream dir from the
        # same --state-dir.
        STREAM_DIR = config.stream_dir(args.state_dir)
        STATE_DIR = args.state_dir

    ensure_cert()

    ip = config.advertised_ip(config.resolve_ip(args.iface, cfg, args.bind_ip))
    bind_ip = args.bind_ip
    # (Not on macOS without root: only the wildcard address may be bound to :443 there.)
    if (bind_ip == "0.0.0.0" and str(cfg["mode"]).lower() == "static" and cfg.get("ip")
            and not (sys.platform == "darwin" and os.geteuid() != 0)):
        bind_ip = cfg["ip"]

    server = Server((bind_ip, args.port), Handler)
    server.aiport_config = {
        "mac": config.resolve_mac(args.iface, args.mac, cfg),
        "platform": args.platform,
        "sysid": args.sysid,
        "hostname": args.hostname,
        "fw_version": args.fw_version,
        "device_id": args.device_id,
        "guid": args.guid,
        "ip": ip,
        "name": cfg.get("name"),
        "console_host": cfg["host"],
        "last_adopt_payload": None,
    }

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_PATH, KEY_PATH)
    server.ssl_context = ctx

    log.info("HTTPS control API listening on %s:%d (mac=%s platform=%s)",
              bind_ip, args.port, server.aiport_config["mac"], args.platform)
    server.serve_forever()


if __name__ == "__main__":
    main()
