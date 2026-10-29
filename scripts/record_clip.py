#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Record a paired camera's stream for piport/replay.py.

    scripts/record_clip.py 02000000000A 60 parked-car
    scripts/record_clip.py rtsp://192.0.2.1:7447/xyz 30 test --device 02000000000C

Copies the video as it comes from Protect (no re-encode) to ~/piport-clips/NAME.mp4 and
writes NAME.json beside it with what the replay needs from the running pi-port's log: the
stream size, the camera's latest ChangeSmartDetectSettings (zones, exclusion zones, lines,
eventStartMSec) and ChangeSmartMotionSettings, and the recording's start time. Given a
deviceID, the RTSP URL is the one pi-port is pulling now (from the same log). Clips stay out
of git: they show the property.
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
from datetime import datetime

LOG = os.path.expanduser(os.environ.get("PIPORT_LOG", "~/piport.log"))
OUT_DIR = os.path.expanduser(os.environ.get("PIPORT_CLIPS", "~/piport-clips"))

_PULL = re.compile(r"starting merged RTSP pull for deviceID=(\w+): (rtsp://\S+)")
_RECEIVED = re.compile(r"<<< received functionName='(\w+)' .*? payload=(\{.*\})\s*$")


def scan_log(lines, device_id):
    """What the log says about a camera: {"url", "width", "height", "fps", "smartDetect",
    "smartMotion"}, each from its latest line (missing keys are absent)."""
    found = {}
    want = device_id.upper()
    for line in lines:
        m = _PULL.search(line)
        if m and m.group(1).upper() == want:
            found["url"] = m.group(2)
            continue
        m = _RECEIVED.search(line)
        if not m:
            continue
        fn = m.group(1)
        if fn not in ("UiStreamControl", "ChangeSmartDetectSettings", "ChangeSmartMotionSettings"):
            continue
        try:
            payload = ast.literal_eval(m.group(2))
        except (ValueError, SyntaxError):
            continue
        if str(payload.get("deviceID", "")).upper() != want:
            continue
        if fn == "UiStreamControl" and payload.get("streaming"):
            for key in ("width", "height", "fps"):
                if payload.get(key):
                    found[key] = payload[key]
        elif fn == "ChangeSmartDetectSettings" and "zones" in payload:
            found["smartDetect"] = payload  # skip partial updates like {'isLprCamera': 0}
        elif fn == "ChangeSmartMotionSettings":
            found["smartMotion"] = payload
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("camera", help="deviceID (looked up in the log) or an rtsp:// URL")
    ap.add_argument("seconds", type=float)
    ap.add_argument("name", help="clip name, without extension")
    ap.add_argument("--device", help="deviceID whose settings to save (with a URL)")
    ap.add_argument("--log", default=LOG)
    ap.add_argument("--out", default=OUT_DIR)
    args = ap.parse_args(argv)

    device = args.device or ("" if args.camera.startswith("rtsp://") else args.camera)
    info = {}
    if device:
        try:
            with open(args.log, errors="replace") as f:
                info = scan_log(f, device)
        except OSError as e:
            print("can't read %s: %s" % (args.log, e), file=sys.stderr)
    url = args.camera if args.camera.startswith("rtsp://") else info.get("url")
    if not url:
        sys.exit("no RTSP URL for %s in %s (is pi-port pulling it?)" % (args.camera, args.log))

    os.makedirs(args.out, exist_ok=True)
    clip = os.path.join(args.out, args.name + ".mp4")
    start = datetime.now().astimezone().isoformat(timespec="milliseconds")
    print("recording %.0f s of %s to %s" % (args.seconds, url, clip))
    rc = subprocess.call(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                          "-rtsp_transport", "tcp", "-t", str(args.seconds), "-i", url,
                          "-map", "0:v:0", "-c", "copy", clip])
    if rc != 0:
        sys.exit("ffmpeg failed (%d)" % rc)
    sidecar = {"deviceID": device or None, "url": url, "start": start}
    sidecar.update({k: info[k] for k in ("width", "height", "fps", "smartDetect", "smartMotion")
                    if k in info})
    with open(os.path.join(args.out, args.name + ".json"), "w") as f:
        json.dump(sidecar, f, indent=1)
    print("wrote %s and %s.json" % (clip, args.name))


if __name__ == "__main__":
    main()
