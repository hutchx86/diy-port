#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Container entrypoint: persistent identity + config in /data, then run_all.py.

On first start this copies the image's aiport.cfg template to /data and
generates a device_id and a Ubiquiti-OUI MAC into /data/identity.json, so the
console sees the same device across restarts and image upgrades (Docker
assigns a fresh random interface MAC to each container start, so the
interface-derived MAC run_all.py would otherwise use is not stable).

Environment (each optional; applied to /data/aiport.cfg on every start):
  AIPORT_CONSOLE    console host[:port] -> [console] host / port
  AIPORT_HOSTNAME   -> [identity] hostname
  AIPORT_DETECTOR   detector backend (read directly by avclient.py)
  AIPORT_DEBUG      "1"/"true" -> --debug
"""
import json
import os
import random
import sys
import uuid

APP = "/app/diyport"
DATA = os.environ.get("AIPORT_DATA", "/data")
CFG = os.path.join(DATA, "aiport.cfg")
IDENTITY = os.path.join(DATA, "identity.json")
UBNT_OUI = "FCECDA"


def set_ini(path, section, key, value):
    """Set section.key in place, keeping comments; add the key/section if missing."""
    with open(path) as f:
        lines = f.read().splitlines()
    out, in_sec, seen, done = [], False, False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_sec and not done:
                out.append(f"{key} = {value}")
                done = True
            in_sec = stripped == f"[{section}]"
            seen = seen or in_sec
        elif in_sec and not done and stripped.split("=", 1)[0].strip() == key and "=" in stripped:
            out.append(f"{key} = {value}")
            done = True
            continue
        out.append(line)
    if in_sec and not done:
        out.append(f"{key} = {value}")
    elif not seen:
        out += ["", f"[{section}]", f"{key} = {value}"]
    with open(path + ".tmp", "w") as f:
        f.write("\n".join(out) + "\n")
    os.replace(path + ".tmp", path)


def load_identity():
    try:
        with open(IDENTITY) as f:
            ident = json.load(f)
        if ident.get("device_id") and ident.get("mac"):
            return ident
    except (OSError, json.JSONDecodeError):
        pass
    ident = {"device_id": str(uuid.uuid4()),
             "mac": UBNT_OUI + "".join(f"{random.randrange(256):02X}" for _ in range(3))}
    with open(IDENTITY + ".tmp", "w") as f:
        json.dump(ident, f)
    os.replace(IDENTITY + ".tmp", IDENTITY)
    print(f"generated identity: device_id={ident['device_id']} mac={ident['mac']}", flush=True)
    return ident


def main():
    os.makedirs(DATA, exist_ok=True)
    if not os.path.exists(CFG):
        with open(os.path.join(APP, "aiport.cfg")) as src, open(CFG, "w") as dst:
            dst.write(src.read())
        print(f"seeded {CFG} from the image template", flush=True)

    console = os.environ.get("AIPORT_CONSOLE", "").strip()
    if console:
        if console.startswith("[") and "]" in console:
            host, _, rest = console[1:].partition("]")
            port = rest[1:] if rest.startswith(":") else ""
        elif console.count(":") == 1:
            host, _, port = console.partition(":")
        else:
            host, port = console, ""
        set_ini(CFG, "console", "host", host)
        set_ini(CFG, "console", "port", port or "7442")
    hostname = os.environ.get("AIPORT_HOSTNAME", "").strip()
    if hostname:
        set_ini(CFG, "identity", "hostname", hostname)

    ident = load_identity()
    args = [sys.executable, os.path.join(APP, "run_all.py"),
            "--config", CFG, "--state-dir", DATA,
            "--mac", ident["mac"], "--device-id", ident["device_id"]]
    if os.environ.get("AIPORT_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
        args.append("--debug")
    os.execv(args[0], args)


if __name__ == "__main__":
    main()
