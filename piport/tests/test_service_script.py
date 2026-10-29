"""scripts/piport_service.sh against a stub pi-port (PIPORT_SERVICE_TEST): settings from the
env file, a restart on touch, a clean stop on Ctrl-C, pi-port in its own process group, a
pi-port left by an earlier start stopped first; and scripts/log_writer.py's rotation."""
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "scripts")
SCRIPT = os.path.join(SCRIPTS, "piport_service.sh")
sys.path.insert(0, SCRIPTS)
import log_writer  # noqa: E402

STUB = textwrap.dedent("""\
    import os, signal, sys, time
    out = os.environ["STUB_OUT"]
    with open(out, "a") as f:
        f.write("start pid=%d pgid=%d args=%s mode=%s\\n" % (
            os.getpid(), os.getpgid(0), " ".join(sys.argv[1:]), os.environ.get("AIPORT_MODE")))
    def stop(sig, frame):
        with open(out, "a") as f:
            f.write("int %d\\n" % os.getpid())
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    while True:
        time.sleep(0.2)
    """)


@unittest.skipUnless(sys.platform == "darwin", "the script uses macOS stat -f")
class TestServiceScript(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        repo = os.path.join(self.dir, "repo")
        os.makedirs(os.path.join(repo, "venv", "bin"))
        os.makedirs(os.path.join(repo, "piport"))
        os.symlink(sys.executable, os.path.join(repo, "venv", "bin", "python"))
        with open(os.path.join(repo, "piport", "run_all.py"), "w") as f:
            f.write(STUB)
        self.env_file = os.path.join(self.dir, "piport.env")
        with open(self.env_file, "w") as f:
            f.write("PIPORT_IFACE=en0\nPIPORT_MAC=AA\nPIPORT_DEVICE_ID=dev\n"
                    "PIPORT_STATE_DIR=/tmp/x\nAIPORT_MODE=one  # a comment\nrm -rf /\n")
        self.trigger = os.path.join(self.dir, "restart")
        self.out = os.path.join(self.dir, "stub.out")
        self.log = os.path.join(self.dir, "service.log")
        self.pidfile = os.path.join(self.dir, "service.pid")
        self.env = dict(os.environ, PIPORT_SERVICE_TEST=repo, PIPORT_ENV_FILE=self.env_file,
                        PIPORT_RESTART_FILE=self.trigger, PIPORT_LOG=self.log,
                        PIPORT_PIDFILE=self.pidfile, STUB_OUT=self.out)
        self.orphan = None
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(["/bin/bash", SCRIPT], env=self.env,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)

    def tearDown(self):
        for p in (self.proc, self.orphan):
            if p is not None and p.poll() is None:
                p.kill()
                p.wait()
        if self.proc is not None:
            self.proc.stdout.close()

    def logged(self):
        try:
            with open(self.log) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def lines(self, prefix):
        try:
            with open(self.out) as f:
                return [line.split() for line in f if line.startswith(prefix)]
        except FileNotFoundError:
            return []

    def wait_for(self, prefix, count, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if len(self.lines(prefix)) >= count:
                return self.lines(prefix)
            time.sleep(0.1)
        self.fail("timed out waiting for %d %r line(s); got %s" % (count, prefix, self.lines("")))

    def test_restart_and_stop(self):
        self.start()
        first = self.wait_for("start", 1)[0]
        pid, pgid = first[1].split("=")[1], first[2].split("=")[1]
        self.assertEqual(pid, pgid)                       # its own process group
        self.assertIn("--iface en0", " ".join(first))
        self.assertIn("mode=one", " ".join(first))        # comment stripped
        time.sleep(1.2)                                   # mtime has 1 s resolution
        with open(self.trigger, "w"):
            pass
        second = self.wait_for("start", 2)[1]
        self.assertEqual(len(self.wait_for("int", 1)), 1)  # the first was asked to stop
        self.proc.send_signal(signal.SIGINT)              # Ctrl-C
        self.proc.wait(timeout=15)
        self.assertEqual(len(self.wait_for("int", 2)), 2)
        self.assertNotEqual(second[1], first[1])
        time.sleep(0.5)                                   # the log writer drains the pipe
        output = self.logged()
        self.assertIn("ignoring line", output)            # the rm line isn't run
        self.assertIn("stopping", output)
        self.assertNotIn("setpgid", output)
        self.assertFalse(os.path.exists(self.pidfile))    # removed on a clean stop

    def test_a_pi_port_left_by_an_earlier_start_is_stopped_first(self):
        stub = os.path.join(self.env["PIPORT_SERVICE_TEST"], "piport", "run_all.py")
        self.orphan = subprocess.Popen([sys.executable, stub, "orphan"], env=self.env,
                                       start_new_session=True)
        self.wait_for("start", 1)
        with open(self.pidfile, "w") as f:
            f.write("%d\n" % self.orphan.pid)
        self.start()
        self.wait_for("int %d" % self.orphan.pid, 1)       # asked to stop
        self.orphan.wait(timeout=10)
        self.wait_for("start", 2)                          # then the new one starts
        time.sleep(0.5)
        self.assertIn("left running by an earlier start", self.logged())
        self.proc.send_signal(signal.SIGINT)
        self.proc.wait(timeout=15)


class TestLogWriter(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "piport.log")

    def test_rotates_at_the_size_and_keeps_the_newest(self):
        log = log_writer.RotatingLog(self.path, max_bytes=100, keep=2)
        for i in range(12):
            log.write(b"line %02d %s\n" % (i, b"x" * 30))   # 40 bytes each
        log.close()
        names = sorted(os.listdir(self.dir))
        self.assertEqual(names, ["piport.log", "piport.log.1", "piport.log.2"])
        # Three 40-byte lines fill a file; the twelfth rotated, so the current one is empty.
        with open(self.path + ".2", "rb") as f:
            self.assertTrue(f.read().startswith(b"line 06"))  # lines 00-05 were dropped
        with open(self.path + ".1", "rb") as f:
            self.assertTrue(f.read().startswith(b"line 09"))
        self.assertEqual(os.path.getsize(self.path), 0)

    def test_a_log_already_over_the_size_is_rotated_on_open(self):
        with open(self.path, "wb") as f:
            f.write(b"y" * 500)
        log_writer.RotatingLog(self.path, max_bytes=100, keep=3).close()
        self.assertEqual(os.path.getsize(self.path + ".1"), 500)
        self.assertEqual(os.path.getsize(self.path), 0)

    def test_copies_stdin_until_every_writer_closes(self):
        proc = subprocess.run([sys.executable, os.path.join(SCRIPTS, "log_writer.py"), self.path],
                              input=b"one\ntwo\n", capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, b"")                 # not a terminal: no echo
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), b"one\ntwo\n")


if __name__ == "__main__":
    unittest.main()
