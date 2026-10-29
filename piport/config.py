#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 hutchx86
"""Shared config + device identity for the AI Port emulator.

Single source of truth read from ``aiport.cfg`` (INI); CLI flags win over cfg
values. Defaults to ``<project root>/aiport.cfg``.
"""
import argparse
import configparser
import hashlib
import ipaddress
import json
import logging
import os
import re
import subprocess
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(HERE, "aiport.cfg")

# Real Ubiquiti OUI; a non-Ubiquiti one makes the controller report "Unknown".
UBNT_OUI = "FCECDA"

# Defaults for any key the cfg file doesn't set.
DEFAULTS = {
    # [identity]
    "mac": "",
    "hostname": "piport",
    "platform": "UVC AI Port",  # canonical Protect type (ucp4 X-Type, /api/info)
    # Discovery TLV 0x0C: Network's catalogue knows the AI Port by SKU "UP-AI-Port",
    # not the canonical type, so the beacon advertises the SKU (Protect accepts either).
    "discovery_platform": "UP-AI-Port",
    # Device name Protect shows / renames; advertised in discovery TLV 0x0B so
    # Network shows the same name (Protect pushes renames via ChangeDeviceSettings).
    "name": "AI Port",
    "sysid": "0xa5f1",
    "fw_version": "5.1.12.67",
    # Discovery advertises a longer fw string than ucp4/avclient's "version".
    "discovery_fw_version": "aiport4G.mt8390.v5.1.12.67.emu.260904.0000",
    "device_id": "fe6488e7-7042-5bcb-ab86-6f0ad1a5baed",
    "guid": "5c2659d4-48f3-16c4-ed83-2fefbad37066",
    # [network]
    "iface": "",                # blank = auto-detect (see resolve_iface)
    "parent_iface": "",         # macvlan parent; blank = [network] iface / auto-detect
    "mode": "dhcp",             # "dhcp" | "static"
    "ip": "",
    "netmask": "255.255.255.0",
    "gateway": "",
    "dns": "",
    # [console]
    "host": "",  # set to your controller's IP
    "port": "7442",
    # [hardware]
    "detector": "auto",         # auto | rknn | edgetpu | cpu | none (see detectors/)
    "detector_model": "",       # blank = the backend's default model in models/
    # [logging]
    "debug": "false",
}

# Env overrides applied after aiport.cfg (container-friendly identity/console config,
# e.g. -e AIPORT_CONSOLE_HOST=...). The detector's own vars are read in detectors/.
ENV_OVERRIDES = {
    "AIPORT_MAC": "mac",
    "AIPORT_HOSTNAME": "hostname",
    "AIPORT_NAME": "name",
    "AIPORT_DEVICE_ID": "device_id",
    "AIPORT_GUID": "guid",
    "AIPORT_SYSID": "sysid",
    "AIPORT_PLATFORM": "platform",
    "AIPORT_DISCOVERY_PLATFORM": "discovery_platform",
    "AIPORT_IFACE": "iface",
    "AIPORT_PARENT_IFACE": "parent_iface",
    "AIPORT_NETWORK_MODE": "mode",
    "AIPORT_NETWORK_IP": "ip",
    "AIPORT_NETWORK_GATEWAY": "gateway",
    "AIPORT_CONSOLE_HOST": "host",
    "AIPORT_CONSOLE_PORT": "port",
    "AIPORT_DEBUG": "debug",
}


def load_config(path=None):
    """Parse aiport.cfg into merged settings; a missing file falls back to DEFAULTS.
    Non-empty ENV_OVERRIDES win over the file."""
    cfg = dict(DEFAULTS)
    path = path or DEFAULT_CONFIG_PATH
    parser = configparser.ConfigParser(interpolation=None)
    if os.path.exists(path):
        parser.read(path)
        for section in parser.sections():
            for key, value in parser.items(section):
                cfg[key] = value.strip()
    for env_name, key in ENV_OVERRIDES.items():
        value = os.environ.get(env_name)
        if value:
            cfg[key] = value
    return cfg


def get_bool(cfg, key):
    return str(cfg.get(key, "")).strip().lower() in ("1", "true", "yes", "on")


# Secrets that show up in logged payloads/headers: camera RTSP credentials
# (OnvifStreamControl streamUrl), the console's login password for this
# device, and adoption tokens.
_REDACTIONS = [
    # URL userinfo, greedily through the last '@' so a password containing '@'
    # cannot leave its tail in the log.
    (re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s'\"]+@"), r"\1<redacted>@"),
    # Query/form credentials (RTSP URLs and snapshot/upload URLs).
    (re.compile(r"([?&](?:password|passwd|username|user|token)=)[^&\s'\"]+", re.I),
     r"\1<redacted>"),
    (re.compile(r"""(['"](?:password|passwd|newPassword|new_password|[\w-]*token|adoptionCode)['"]\s*:\s*['"])[^'"]+""", re.I),
     r"\1<redacted>"),
    # "token changed (old -> new)" before the generic token-value rule.
    (re.compile(r"(\btoken changed \()[^)]*"), r"\1<redacted>"),
    # "token: X" / "newPassword: X" (colon or equals), including short values.
    (re.compile(r"(\b(?:token|newPassword|new_password)\b\s*[:=]\s*)[^\s,;)}\]]+", re.I),
     r"\1<redacted>"),
    # A long token-like value after a space ("token FAKE... already used").
    (re.compile(r"(\b(?:token|newPassword|new_password)\b[^A-Za-z0-9<(]{1,12})[A-Za-z0-9_+/.=-]{12,}"), r"\1<redacted>"),
]


def redact(text):
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class RedactFilter(logging.Filter):
    """Rewrites each record's final message through redact()."""

    def filter(self, record):
        message = record.getMessage()
        redacted = redact(message)
        if redacted != message:
            record.msg, record.args = redacted, None
        return True


def apply_logging(debug=False):
    logging.basicConfig(level=logging.DEBUG if debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # On the handlers, not the root logger: logger filters skip records
    # propagated from child loggers.
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, RedactFilter) for f in handler.filters):
            handler.addFilter(RedactFilter())


def sysid_int(cfg):
    return int(str(cfg.get("sysid", "0xa5f1")), 0)


def add_common_flags(ap):
    ap.add_argument("--config", default=None,
                    help="path to aiport.cfg (default: <project root>/aiport.cfg)")
    ap.add_argument("--debug", action="store_true", default=None,
                    help="enable debug logging (overrides [logging] debug=true)")


def load_config_and_logging(argv=None):
    """Pre-parse --config/--debug (position-independent), load cfg and apply the
    debug log level; callers must also declare both via add_common_flags()."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", default=None)
    pre.add_argument("--debug", action="store_true", default=None)
    known, _ = pre.parse_known_args(argv if argv is not None else None)
    cfg = load_config(known.config)
    debug = known.debug if known.debug is not None else get_bool(cfg, "debug")
    apply_logging(debug)
    return cfg


def _run(cmd):
    """stdout of a short command, or "" if it's missing/fails."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _ifconfig_field(iface, key):
    """First value after `key` in BSD/macOS `ifconfig <iface>` (no /sys, no `ip`)."""
    for line in _run(["ifconfig", iface]).splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == key:
            return fields[1]
    return ""


def _iface_mac(iface):
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip().upper().replace(":", "")
    except OSError:
        pass
    # macOS masks `ifconfig` ether as 02:00:00:00:00:00 for most processes; networksetup doesn't.
    out = _run(["networksetup", "-getmacaddress", iface]).split()
    mac = out[2] if len(out) > 2 and out[:2] == ["Ethernet", "Address:"] else ""
    mac = mac or _ifconfig_field(iface, "ether")
    mac = mac.upper().replace(":", "")
    return "" if mac == "020000000000" else mac


def _default_iface():
    """Best-effort auto-detect: the default-route interface, else the first
    non-loopback up interface. Returns "" if unknown."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                fields = line.split()
                # dest 0.0.0.0 with RTF_UP (0x1) = the default-route device
                if len(fields) >= 4 and fields[1] == "00000000" and int(fields[3], 16) & 0x1:
                    return fields[0]
    except (OSError, ValueError):
        pass
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if name == "lo":
                continue
            try:
                with open(f"/sys/class/net/{name}/operstate") as fh:
                    if fh.read().strip() in ("up", "unknown"):
                        return name
            except OSError:
                continue
    except OSError:
        pass
    # macOS/BSD: no /proc or /sys, ask the routing table.
    for line in _run(["route", "-n", "get", "default"]).splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "interface" and value.strip():
            return value.strip()
    return ""


def resolve_iface(iface_override=None, cfg=None):
    """Interface to bind/derive identity from. Precedence: explicit --iface,
    then cfg [network] iface, then auto-detect."""
    cfg = cfg or {}
    explicit = iface_override or cfg.get("iface") or ""
    return explicit or _default_iface() or "eth0"


def resolve_parent_iface(cfg=None):
    """Physical interface a macvlan instance is created on top of. Precedence:
    cfg [network] parent_iface, then [network] iface, then auto-detect."""
    cfg = cfg or {}
    return cfg.get("parent_iface") or resolve_iface(None, cfg)


def resolve_mac(iface, mac_override=None, cfg=None):
    """12-hex-char MAC (uppercase, no separators). Precedence: --mac, cfg
    [identity] mac, then real iface MAC with the Ubiquiti OUI prepended."""
    cfg = cfg or {}
    raw = mac_override or cfg.get("mac") or ""
    if raw:
        return raw.replace(":", "").replace("-", "").upper()
    real = _iface_mac(iface)
    if real:
        return UBNT_OUI + real[6:]
    return "000000000000"


def _iface_ip(iface):
    """The interface's currently-assigned IPv4, or None."""
    try:
        out = subprocess.run(["ip", "-4", "-json", "addr", "show", iface],
                             capture_output=True, text=True, timeout=5)
        data = json.loads(out.stdout or "[]")
        for link in data:
            for addr in link.get("addr_info", []):
                if addr.get("family") == "inet":
                    return addr["local"]
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError, KeyError):
        pass
    # BSD/macOS ifconfig; reject busybox's "addr:1.2.3.4" form rather than misparse it.
    ip = _ifconfig_field(iface, "inet")
    try:
        return str(ipaddress.IPv4Address(ip))
    except ValueError:
        return None


def resolve_ip(iface, cfg=None, bind_ip=None):
    """IP to report/bind to. Precedence: --bind-ip, then static cfg ip (when
    mode == static), then the interface's live IP; "0.0.0.0" as last resort."""
    if bind_ip and bind_ip != "0.0.0.0":
        return bind_ip
    cfg = cfg or {}
    if str(cfg.get("mode", "")).lower() == "static" and cfg.get("ip"):
        return cfg["ip"]
    return _iface_ip(iface) or "0.0.0.0"


def advertised_ip(ip):
    """The IP to tell Protect, when that isn't the one bound: AIPORT_ADVERTISE_IP, for a
    container behind NAT whose :443 is published on a host address (compose.macos.yaml);
    otherwise `ip`. Only what's reported changes, never what's bound."""
    return (os.environ.get("AIPORT_ADVERTISE_IP") or "").strip() or ip


def netmask_to_prefix(netmask):
    """Dotted-quad netmask -> CIDR prefix length (255.255.255.0 -> 24)."""
    if not netmask:
        return None
    try:
        octets = [int(x) for x in str(netmask).split(".")]
        if len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
            return None
        value = sum(o << (8 * (3 - i)) for i, o in enumerate(octets))
        inverted = (~value) & 0xFFFFFFFF
        if inverted & (inverted + 1):  # non-contiguous mask
            return None
        return bin(value).count("1")
    except ValueError:
        return None


def atomic_write_json(path, obj):
    """Write obj as JSON atomically (tmp + os.replace) so readers never see a torn write."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def _tmpfs_base():
    """First writable RAM-backed dir on the box (/dev/shm, then /run), falling
    back to the OS temp dir."""
    for base in ("/dev/shm", "/run"):
        if os.path.isdir(base) and os.access(base, os.W_OK):
            return base
    return tempfile.gettempdir()


def stream_dir(state_dir=None):
    """RAM-only dir for transient per-stream snapshot JPEGs; never persistent.
    --state-dir maps to a hash of its absolute path."""
    base = _tmpfs_base()
    if state_dir:
        key = hashlib.sha1(os.path.abspath(state_dir).encode()).hexdigest()[:12]
    else:
        key = "default"
    return os.path.join(base, "aiport-streams", key)


def pinned_fw_version(state_dir=None):
    """Firmware version pinned by a simulated UpdateFirmwareRequest, or None.

    avclient.py writes ``fw_version.json`` here on the update (ack + fake reboot,
    no flash); every stage must pass the same ``--state-dir`` to read it. Without
    one, all stages share the piport/ directory.
    """
    path = os.path.join(state_dir or HERE, "fw_version.json")
    try:
        with open(path) as f:
            version = json.load(f).get("version")
        return str(version) if version else None
    except (OSError, json.JSONDecodeError):
        return None


def effective_fw_version(configured, state_dir=None):
    """The version to report: a pinned update version if present, else configured."""
    return pinned_fw_version(state_dir) or configured


def pin_fw_version(state_dir, version):
    """Persist the version a simulated UpdateFirmwareRequest 'installed'."""
    atomic_write_json(os.path.join(state_dir or HERE, "fw_version.json"),
                      {"version": version})


def device_name(state_dir=None, default=None):
    """The device's advertised name (discovery TLV 0x0B), or the configured
    default. Protect pushes the user's rename via ChangeDeviceSettings; persisting
    it here lets the discovery responder report the same name Network shows."""
    try:
        with open(os.path.join(state_dir or HERE, "device_name")) as f:
            name = f.read().strip()
        return name or (default or DEFAULTS["name"])
    except OSError:
        return default or DEFAULTS["name"]


def set_device_name(state_dir, name):
    path = os.path.join(state_dir or HERE, "device_name")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(name)
    os.replace(tmp, path)
