# SPDX-License-Identifier: AGPL-3.0-or-later
"""Host load once a minute, beside each camera's frame stats: who used the CPU and how busy
the GPU and neural accelerator were. Live 17:59 every camera's pass time rose from ~2 ms to
36-59 ms at once while three cameras were busy and Frigate shared the Mac; the frame stats
alone couldn't say who was using it.

CPU per group, on macOS and Linux, comes from each process's CPU time (ps), as a delta over
the minute: pi-port (this instance's process tree: its ffmpeg pulls and helpers), Frigate
(processes whose command line names it, and their children), OrbStack on macOS and Docker
containers on Linux (the camera proxies), and everything else. 1.0 = one core busy for the
whole minute.

The accelerators: on macOS, powermetrics (a one-second sample: cores and GPU busy %, CPU, GPU
and ANE power), which needs root, as pi-port runs; on Linux, cores busy from /proc/stat, the
RK3588 NPU's load (/sys/kernel/debug/rknpu/load, root) and a devfreq GPU's load where they can
be read. What a host can't report is left out of the line.
"""
import glob
import logging
import os
import plistlib
import re
import subprocess
import sys
import threading
import time

log = logging.getLogger("aiport-host")

INTERVAL_S = 60.0
POWERMETRICS = "/usr/bin/powermetrics"
RKNPU_LOAD = "/sys/kernel/debug/rknpu/load"
# Process groups by a tree root's command line (the first match wins); pi-port's own tree
# is marked before these.
GROUP_ROOTS = (
    ("Frigate", re.compile(r"Fregata\.app/Contents/MacOS/Fregata|(^|[/ ])frigate([ .:]|$)")),
    ("OrbStack", re.compile(r"OrbStack(\.app/Contents/MacOS/OrbStack| Helper)")),
    ("containers", re.compile(r"containerd-shim")),
)


def enabled():
    """AIPORT_HOST_STATS (auto|0|1): auto is on for macOS and Linux."""
    flag = os.environ.get("AIPORT_HOST_STATS", "auto").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    if flag in ("1", "true", "yes", "on"):
        return True
    return sys.platform in ("darwin", "linux")


def parse_cputime(text):
    """Seconds from ps's TIME column: [[dd-]hh:]mm:ss[.cc] (macOS keeps hundredths)."""
    days, _, rest = text.strip().rpartition("-")
    seconds = 0.0
    for p in rest.split(":"):
        seconds = seconds * 60 + float(p)
    return seconds + (int(days) * 86400 if days else 0)


def parse_ps(out):
    """{pid: (ppid, cpu seconds, command line)} from `ps -A -o pid=,ppid=,time=,args=`."""
    procs = {}
    for line in out.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 4:
            continue
        try:
            procs[int(fields[0])] = (int(fields[1]), parse_cputime(fields[2]), fields[3])
        except ValueError:
            continue
    return procs


def read_processes():
    """{pid: (ppid, cpu seconds, args)} from ps, or {} without it: a slim container image has
    no ps, and the host stats thread died on it (the line then just leaves out the groups)."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,time=,args="], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_ps(out)


def group_of(procs, own_root):
    """{pid: group}: pi-port for own_root's tree, then GROUP_ROOTS' trees; the rest is other."""
    children = {}
    for pid, (ppid, _, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    groups = {}

    def mark(root, name):
        stack = [root]
        while stack:
            pid = stack.pop()
            if pid in groups:
                continue
            groups[pid] = name
            stack.extend(children.get(pid, ()))
    if own_root in procs:
        mark(own_root, "pi-port")
    for name, pattern in GROUP_ROOTS:
        for pid in sorted(procs):
            if pid not in groups and pattern.search(procs[pid][2]):
                mark(pid, name)
    return groups


def cpu_by_group(before, after, own_root, seconds):
    """{group: cores busy on average} between two read_processes() samples `seconds` apart.
    A process that started in between counts from zero; one that ended is left out."""
    groups = group_of(after, own_root)
    cores = {}
    for pid, (_, cpu, _) in after.items():
        prev = before.get(pid)
        used = cpu - (prev[1] if prev and prev[1] <= cpu else 0.0)
        name = groups.get(pid, "other")
        cores[name] = cores.get(name, 0.0) + used / seconds
    return cores


# --- macOS: powermetrics -------------------------------------------------------------

def _find(node, key):
    """First value under `key` anywhere in a nested plist."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        node = list(node.values())
    if isinstance(node, list):
        for value in node:
            found = _find(value, key)
            if found is not None:
                return found
    return None


def parse_powermetrics(data):
    """{"cpu_busy", "gpu_busy" (0-1), "cpu_w", "gpu_w", "ane_w" (watts)} from one plist
    sample; keys that aren't in it are left out."""
    sample = plistlib.loads(data.split(b"\0")[0].strip())
    out = {}
    processor = sample.get("processor") or {}
    for key, name in (("cpu_power", "cpu_w"), ("gpu_power", "gpu_w"), ("ane_power", "ane_w")):
        value = processor.get(key, _find(sample, key))
        if isinstance(value, (int, float)):
            out[name] = value / 1000.0      # milliwatts
    idle = [c.get("idle_ratio") for cl in processor.get("clusters") or []
            for c in cl.get("cpus") or [] if isinstance(c.get("idle_ratio"), (int, float))]
    if idle:
        out["cpu_busy"] = 1.0 - sum(idle) / len(idle)
    gpu = sample.get("gpu") or {}
    if isinstance(gpu.get("idle_ratio"), (int, float)):
        out["gpu_busy"] = 1.0 - gpu["idle_ratio"]
    return out


# powermetrics needs root. Not running as root (the launchd agent runs as the user), it's run
# through `sudo -n`, which works only with a sudoers rule for it (see scripts/macos_launchd.sh);
# without one the GPU and power figures are left out from the first failure on.
_powermetrics_denied = []


def _sample_macos():
    if _powermetrics_denied or not os.path.exists(POWERMETRICS):
        return {}
    cmd = [POWERMETRICS, "-n", "1", "-i", "1000", "-f", "plist",
           "-s", "cpu_power,gpu_power,ane_power"]
    if os.geteuid() != 0:
        cmd = ["/usr/bin/sudo", "-n"] + cmd
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=20)
        if proc.returncode != 0 and os.geteuid() != 0:
            _powermetrics_denied.append(True)
            log.info("host stats: powermetrics needs root and sudo -n wasn't allowed; "
                     "GPU and power figures are off")
            return {}
        return parse_powermetrics(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, plistlib.InvalidFileException):
        log.debug("powermetrics sample failed", exc_info=True)
        return {}


# --- Linux: /proc and /sys -----------------------------------------------------------

def parse_proc_stat(text):
    """(busy, total) jiffies from /proc/stat's aggregate "cpu" line."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            values = [int(v) for v in line.split()[1:]]
            idle = values[3] + (values[4] if len(values) > 4 else 0)   # idle + iowait
            return sum(values) - idle, sum(values)
    return None


def parse_rknpu_load(text):
    """Mean NPU core load 0-1 from "NPU load:  Core0: 12%, Core1:  0%, Core2:  3%,"."""
    loads = [int(v) for v in re.findall(r"Core\d+:\s*(\d+)%", text)]
    return sum(loads) / len(loads) / 100.0 if loads else None


def parse_devfreq_load(text):
    """GPU load 0-1 from a devfreq "load" file ("37@800000000Hz", or just "37")."""
    m = re.match(r"\s*(\d+)", text)
    return int(m.group(1)) / 100.0 if m else None


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


class _LinuxSampler:
    """cores busy over the last interval (/proc/stat deltas) plus instantaneous NPU/GPU load."""

    def __init__(self):
        text = _read("/proc/stat")
        self._last = parse_proc_stat(text) if text else None

    def sample(self):
        out = {}
        text = _read("/proc/stat")
        now = parse_proc_stat(text) if text else None
        if now and self._last and now[1] > self._last[1]:
            out["cpu_busy"] = (now[0] - self._last[0]) / (now[1] - self._last[1])
        self._last = now
        npu = _read(RKNPU_LOAD)
        if npu and parse_rknpu_load(npu) is not None:
            out["npu_busy"] = parse_rknpu_load(npu)
        for path in sorted(glob.glob("/sys/class/devfreq/*gpu*/load")):
            load = parse_devfreq_load(_read(path) or "")
            if load is not None:
                out["gpu_busy"] = load
                break
        return out


# --- the line -------------------------------------------------------------------------

def format_line(seconds, load, cores, ncpu, accel):
    """The once-a-minute log line."""
    order = ["pi-port", "Frigate", "OrbStack", "containers", "other"]
    names = order + sorted(k for k in cores if k not in order)
    parts = ["host over %.0f s: load %.1f/%.1f/%.1f" % ((seconds,) + tuple(load)),
             "CPU %.1f of %d cores (%s)" % (sum(cores.values()), ncpu, ", ".join(
                 "%s %.2f" % (n, cores[n]) for n in names if n in cores))]
    for key, label in (("cpu_busy", "cores"), ("gpu_busy", "GPU"), ("npu_busy", "NPU")):
        if key in accel:
            parts.append("%s %.0f%% busy" % (label, 100 * accel[key]))
    watts = ["%s %.1f W" % (label, accel[key]) for key, label in
             (("cpu_w", "CPU"), ("gpu_w", "GPU"), ("ane_w", "ANE")) if key in accel]
    if watts:
        parts.append("power " + ", ".join(watts))
    return ", ".join(parts)


def start(own_root=None, interval=INTERVAL_S):
    """Log format_line() every `interval` seconds from a daemon thread. own_root: the root of
    this instance's process tree (run_all.py, avclient's parent)."""
    own_root = own_root or os.getppid()
    sample_accel = _LinuxSampler().sample if sys.platform == "linux" else _sample_macos

    def run():
        before, t0 = read_processes(), time.monotonic()
        while True:
            time.sleep(interval)
            try:
                after, t1 = read_processes(), time.monotonic()
                elapsed = t1 - t0
                cores = cpu_by_group(before, after, own_root, elapsed)
                before, t0 = after, t1
                log.info(format_line(elapsed, os.getloadavg(), cores, os.cpu_count() or 1,
                                     sample_accel()))
            except Exception:
                log.exception("host stats failed")

    thread = threading.Thread(target=run, name="host-stats", daemon=True)
    thread.start()
    return thread
