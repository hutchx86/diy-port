#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Minimal dev-box system monitor for the board running this emulator
(NOT part of the AI Port protocol). Shows CPU/memory/GPU/accelerator usage in
a browser. Stdlib-only (http.server).

Sources (board-specific ones are simply n/a elsewhere):
  CPU:   /proc/stat
  Mem:   /proc/meminfo
  GPU:   /sys/devices/platform/fb000000.gpu/utilisation (RK3588 Mali, 0-100)
  NPU:   /sys/kernel/debug/rknpu/load (RK3588, three cores, needs root)
  Coral: USB ids / /dev/apex_0 -- presence and link speed only; the Edge TPU
         exposes no load counter
  Board: /proc/device-tree/model
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
# Shelled out to (not imported) so this stays stdlib-only; also inherits this process's root privileges.
INSTANCE_MANAGER = os.path.join(HERE, "instance_manager.py")

GPU_UTIL_PATH = "/sys/devices/platform/fb000000.gpu/utilisation"
NPU_LOAD_PATH = "/sys/kernel/debug/rknpu/load"

_NPU_CORE_RE = re.compile(r"Core\d+:\s*(\d+)%")
BOARD_MODEL_PATH = "/proc/device-tree/model"
# Same ids as detectors/edgetpu.py (not imported: that pulls in numpy).
_CORAL_USB_IDS = {("1a6e", "089a"): False, ("18d1", "9302"): True}  # -> firmware loaded

# Two /proc/stat samples needed for a CPU delta; keep previous across requests.
_prev_cpu_total = None
_prev_cpu_idle = None
# Per-core prev samples, keyed by /proc/stat's "cpuN" label so the core count isn't hardcoded.
_prev_core_total = {}
_prev_core_idle = {}


def _read_file(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def _cpu_percent():
    global _prev_cpu_total, _prev_cpu_idle
    line = _read_file("/proc/stat")
    if not line:
        return None
    first_line = line.splitlines()[0]  # aggregate "cpu ..." line, all cores
    fields = [int(x) for x in first_line.split()[1:]]
    idle = fields[3] + fields[4]  # idle + iowait
    total = sum(fields)
    pct = None
    if _prev_cpu_total is not None:
        dt = total - _prev_cpu_total
        di = idle - _prev_cpu_idle
        if dt > 0:
            pct = round(100.0 * (dt - di) / dt, 1)
    _prev_cpu_total, _prev_cpu_idle = total, idle
    return pct


def _cpu_core_percents():
    text = _read_file("/proc/stat")
    if not text:
        return None
    percents = []
    for line in text.splitlines():
        if not line.startswith("cpu") or line[3] not in "0123456789":
            continue
        label, *rest = line.split()
        fields = [int(x) for x in rest]
        idle = fields[3] + fields[4]
        total = sum(fields)
        prev_total = _prev_core_total.get(label)
        prev_idle = _prev_core_idle.get(label)
        pct = None
        if prev_total is not None:
            dt = total - prev_total
            di = idle - prev_idle
            if dt > 0:
                pct = round(100.0 * (dt - di) / dt, 1)
        _prev_core_total[label] = total
        _prev_core_idle[label] = idle
        percents.append(pct)
    return percents or None


def _mem_stats():
    text = _read_file("/proc/meminfo")
    if not text:
        return None, None, None
    values = {}
    for line in text.splitlines():
        parts = line.split(":")
        if len(parts) == 2:
            values[parts[0].strip()] = int(parts[1].strip().split()[0])  # kB
    total_kb = values.get("MemTotal")
    avail_kb = values.get("MemAvailable")
    if total_kb is None or avail_kb is None:
        return None, None, None
    used_kb = total_kb - avail_kb
    pct = round(100.0 * used_kb / total_kb, 1) if total_kb else None
    return round(used_kb / 1024), round(total_kb / 1024), pct  # MB, MB, %


def _gpu_percent():
    text = _read_file(GPU_UTIL_PATH)
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def _npu_percent():
    text = _read_file(NPU_LOAD_PATH)
    if not text:
        return None
    cores = [int(m) for m in _NPU_CORE_RE.findall(text)]
    if not cores:
        return None
    return {"average": round(sum(cores) / len(cores), 1), "cores": cores}


def _coral_status():
    """{"bus", "speed_mbps", "firmware_loaded"} for the first Coral found, or None."""
    for dev in glob.glob("/sys/bus/usb/devices/*"):
        vendor = _read_file(os.path.join(dev, "idVendor"))
        product = _read_file(os.path.join(dev, "idProduct"))
        if vendor is None or product is None:
            continue
        loaded = _CORAL_USB_IDS.get((vendor.strip(), product.strip()))
        if loaded is None:
            continue
        speed = (_read_file(os.path.join(dev, "speed")) or "").strip()
        return {"bus": "usb", "speed_mbps": int(float(speed)) if speed else None,
                "firmware_loaded": loaded}
    if os.path.exists("/dev/apex_0"):
        return {"bus": "pcie", "speed_mbps": None, "firmware_loaded": True}
    return None


def _board_model():
    text = _read_file(BOARD_MODEL_PATH)
    return text.strip("\0\n ") if text else None


def collect_stats():
    mem_used_mb, mem_total_mb, mem_pct = _mem_stats()
    return {
        "timestamp": time.time(),
        "cpu_percent": _cpu_percent(),
        "cpu_cores": _cpu_core_percents(),
        "mem_used_mb": mem_used_mb,
        "mem_total_mb": mem_total_mb,
        "mem_percent": mem_pct,
        "gpu_percent": _gpu_percent(),
        "npu": _npu_percent(),
        "coral": _coral_status(),
        "board": _board_model(),
    }


def list_instances():
    try:
        out = subprocess.run([sys.executable, INSTANCE_MANAGER, "list"],
                              capture_output=True, text=True, timeout=10)
        return json.loads(out.stdout or "[]")
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return []


def create_instance(name):
    """Blocks up to ~20s for the DHCP lease; safe because each request runs
    on its own thread."""
    proc = subprocess.run([sys.executable, INSTANCE_MANAGER, "create", name],
                           capture_output=True, text=True, timeout=35)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "unknown error").strip())
    return json.loads(proc.stdout)


def destroy_instance(name):
    proc = subprocess.run([sys.executable, INSTANCE_MANAGER, "destroy", name],
                           capture_output=True, text=True, timeout=15)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "unknown error").strip())


def _load_page():
    """Read the static web UI (webui.html, kept beside this module)."""
    with open(os.path.join(HERE, "webui.html"), encoding="utf-8") as f:
        return f.read()


PAGE_HTML = _load_page()
# Root-level instance management is localhost-only by default. Status polling can
# still be exposed on the LAN; opt in explicitly if remote management is wanted.
ALLOW_REMOTE_INSTANCE_CONTROL = os.environ.get(
    "AIPORT_WEBUI_ALLOW_REMOTE", "").strip().lower() in ("1", "true", "yes", "on")
MAX_BODY_BYTES = 64 * 1024


class Handler(BaseHTTPRequestHandler):
    timeout = 10

    def log_message(self, *args):
        pass  # polled every second, not worth logging

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = PAGE_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/stats":
            self._send_json(collect_stats())
        elif self.path == "/api/instances":
            # Hide physical-mode instances (permanent, systemd-managed): no misleading "Tear down" button.
            self._send_json([i for i in list_instances() if i.get("mode") != "physical"])
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path not in ("/api/instances/create", "/api/instances/destroy"):
            self.send_response(404)
            self.end_headers()
            return
        if (not ALLOW_REMOTE_INSTANCE_CONTROL
                and self.client_address[0] not in ("127.0.0.1", "::1")):
            self._send_json({"error": "instance management is localhost-only"}, status=403)
            return
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length < 0 or length > MAX_BODY_BYTES:
                raise ValueError("request body too large")
            payload = json.loads(self.rfile.read(length) or b"{}")
            name = str(payload.get("name", "")).strip()
            if not name:
                raise ValueError("name is required")
            if self.path == "/api/instances/create":
                result = create_instance(name)
            else:
                destroy_instance(name)
                result = {"ok": True}
            self._send_json(result)
        except Exception as e:
            self._send_json({"error": str(e)}, status=400)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind-ip", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind_ip, args.port), Handler)
    print(f"sysinfo server listening on {args.bind_ip}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
