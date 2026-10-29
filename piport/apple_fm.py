# SPDX-License-Identifier: AGPL-3.0-or-later
"""Vehicle type and colour from Apple's on-device foundation model (macOS 27+).

COCO has no SUV or van, and the HSV colour vote calls a black car in daylight blue or gray
(live 15:13: a black Ford Explorer went out as car/truck and blue). On 40 crops from the
saved clips the system model named the type right 72% of the time and the colour 85%, at
1-3 s a crop and almost no memory of ours: the model is the system's, already loaded for
Apple Intelligence. avclient asks it about a vehicle track's best crops in the background
and votes over the answers.

The model runs in the logged-in user's session, so when pi-port runs as root (sudo) the
helper is started as that user (launchctl asuser + sudo -u). The helper is built with
swiftc from apple/vehicle_attrs.swift into apple/build/ on first use.
"""
import json
import logging
import os
import shutil
import subprocess
import sys
import threading

import enrich

log = logging.getLogger("aiport-apple-fm")

HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.path.join(HERE, "apple", "vehicle_attrs.swift")
BUILD_DIR = os.path.join(HERE, "apple", "build")
REQUEST_TIMEOUT_S = 20.0

# Protect's vehicleType vocabulary (enrich.VEHICLE_TYPES' targets plus suv and van).
TYPE_WORDS = {
    "car": "car", "sedan": "car", "hatchback": "car", "coupe": "car", "wagon": "car",
    "suv": "suv", "crossover": "suv",
    "truck": "truck", "pickup": "truck",
    "van": "van", "minivan": "van",
    "bus": "bus",
    "motorcycle": "motorcycle", "motorbike": "motorcycle",
    "bike": "bike", "bicycle": "bike",
    # Not a Protect type: a camper or towed trailer gets no vehicleType at all.
    "trailer": "trailer", "camper": "trailer", "rv": "trailer",
}
COLOR_WORDS = {"grey": "gray", "silver": "gray"}


def parse_answer(answer):
    """(vehicle type or None, colour or None) from the model's "<type> <colour>" answer;
    "trailer" is returned as the type so callers can drop it."""
    words = [w.strip(".,;:!\"'").lower() for w in (answer or "").split()]
    vtype = next((TYPE_WORDS[w] for w in words if w in TYPE_WORDS), None)
    color = None
    for w in words:
        w = COLOR_WORDS.get(w, w)
        if w in enrich.PROTECT_COLORS:
            color = w
            break
    return vtype, color


def enabled():
    """AIPORT_VEHICLE_FM (auto|0|1): auto is on for macOS when vehicle attributes are on."""
    flag = os.environ.get("AIPORT_VEHICLE_FM", "auto").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return sys.platform == "darwin" and enrich.vehicle_attrs_enabled()


def _build(binary):
    """Compile the helper if it's missing or older than its source; True if usable."""
    try:
        if os.path.exists(binary) and os.path.getmtime(binary) >= os.path.getmtime(SOURCE):
            return True
        swiftc = shutil.which("swiftc")
        if not swiftc:
            log.warning("Apple vehicle attributes: no swiftc (install the Xcode command line "
                        "tools); using the built-in type and colour")
            return False
        os.makedirs(os.path.dirname(binary), exist_ok=True)
        log.info("Apple vehicle attributes: building %s", binary)
        subprocess.run([swiftc, "-parse-as-library", "-O", SOURCE, "-o", binary],
                       check=True, capture_output=True, text=True, timeout=600)
        os.chmod(binary, 0o755)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or exc
        log.warning("Apple vehicle attributes: helper build failed (%s); using the built-in "
                    "type and colour", str(detail)[-300:])
        return False


def _as_session_user(cmd):
    """Run as the user who started pi-port with sudo, in their login session, where the
    system model lives; unchanged when not root."""
    uid, user = os.environ.get("SUDO_UID"), os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and uid and user:
        return ["launchctl", "asuser", uid, "sudo", "-u", user, "--"] + cmd
    return cmd


class VehicleClassifier:
    """One long-running helper process; classify() is serialised and thread-safe."""

    def __init__(self, binary=None):
        self.binary = binary or os.path.join(BUILD_DIR, "vehicle_attrs")
        self._lock = threading.Lock()
        self._proc = None
        self.available = None   # None until first use; then True/False for good

    def _start(self):
        if not _build(self.binary):
            self.available = False
            return
        try:
            self._proc = subprocess.Popen(
                _as_session_user([self.binary]), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
            first = self._readline(REQUEST_TIMEOUT_S)
        except OSError as exc:
            first = "unavailable: %s" % exc
        if first != "ready":
            log.warning("Apple vehicle attributes unavailable (%s); using the built-in type "
                        "and colour", (first or "no answer").strip())
            self._stop()
            self.available = False
            return
        log.info("Apple vehicle attributes: on-device model ready")
        self.available = True

    def _readline(self, timeout):
        """One line from the helper, or None after `timeout` (the helper is then stopped)."""
        out = []
        reader = threading.Thread(target=lambda: out.append(self._proc.stdout.readline()),
                                  daemon=True)
        reader.start()
        reader.join(timeout)
        if reader.is_alive() or not out or not out[0]:
            return None
        return out[0].strip()

    def _stop(self):
        if self._proc is not None:
            try:
                self._proc.kill()
            except OSError:
                pass
            self._proc = None

    def classify(self, image_path):
        """(vehicle type or None, colour or None, ms) for a crop file, or None when the
        model is unavailable or didn't answer."""
        with self._lock:
            if self.available is None:
                self._start()
            if not self.available:
                return None
            if self._proc is None or self._proc.poll() is not None:
                self._start()
                if not self.available:
                    return None
            try:
                self._proc.stdin.write(image_path + "\n")
                self._proc.stdin.flush()
            except OSError:
                self._stop()
                return None
            line = self._readline(REQUEST_TIMEOUT_S)
            if line is None:
                log.warning("Apple vehicle attributes: no answer in %.0f s, restarting the "
                            "helper", REQUEST_TIMEOUT_S)
                self._stop()
                return None
        try:
            reply = json.loads(line)
        except ValueError:
            return None
        if "answer" not in reply:
            log.debug("Apple vehicle attributes: %s", reply.get("error"))
            return None
        vtype, color = parse_answer(reply["answer"])
        return vtype, color, reply.get("ms")
