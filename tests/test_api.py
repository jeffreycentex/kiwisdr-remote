"""Service layer, session policy, HTTP API, ctl client and headless mode.

Run:  python -m unittest discover -s tests -v
"""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import wave

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_kiwi import FakeKiwi  # noqa: E402
from kiwiremote import ctl  # noqa: E402
from kiwiremote.api import ApiServer  # noqa: E402
from kiwiremote.service import ApiError, KiwiService, SessionPolicy, is_local_address  # noqa: E402


def wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestLocality(unittest.TestCase):
    def test_addresses(self):
        for a in ("127.0.0.1", "::1", "10.1.2.3", "192.168.1.20", "172.16.5.5", "fd00::5",
                  "169.254.1.1", "fe80::1%eth0", "::ffff:192.168.1.2"):
            self.assertTrue(is_local_address(a), a)
        for a in ("8.8.8.8", "2001:4860:4860::8888", "", None, "kiwi.example.net"):
            self.assertFalse(is_local_address(a), a)


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.clock = FakeClock()
        self.kiwis = []

    def kiwi(self, **kw):
        k = FakeKiwi(**kw)
        self.kiwis.append(k)
        return k

    def service(self, **policy):
        self.svc = KiwiService(policy=SessionPolicy(**policy), compression=False,
                               record_dir=self.tmp, clock=self.clock, start_ticker=False)
        return self.svc

    def tearDown(self):
        if getattr(self, "svc", None):
            self.svc.shutdown()
        for k in self.kiwis:
            k.stop()


class TestSessionPolicy(ServiceCase):
    def test_local_server_is_exempt(self):
        k = self.kiwi()
        svc = self.service()
        st = svc.connect("127.0.0.1:%d" % k.port, wait=8)
        self.assertEqual(st["state"], "connected")
        s = st["session"]
        self.assertTrue(s["local"])
        self.assertIsNone(s["limit_s"])
        self.assertIsNone(s["reset_interval_s"])

    def test_public_limit_closes_recording_and_disconnects(self):
        k = self.kiwi()
        svc = self.service(exempt_local=False)
        svc.connect("127.0.0.1:%d" % k.port, wait=8)
        self.assertEqual(svc.session_info()["limit_s"], 45 * 60)
        rec = svc.start_recording(name="net")
        self.clock.advance(45 * 60 - 1)
        svc.tick()
        self.assertTrue(svc.radio.connected)
        self.clock.advance(2)
        svc.tick()
        self.assertFalse(svc.radio.connected)
        self.assertFalse(svc.recording_info()["active"])
        self.assertEqual(svc.session_info()["ended"], "session limit")
        self.assertTrue(os.path.exists(rec["path"]))
        svc.tick()                                       # no reconnect afterwards
        time.sleep(0.5)
        self.assertEqual(k.connections, 1)

    def test_password_session_keeps_going_with_resets(self):
        k = self.kiwi(password="s3cret")
        svc = self.service(exempt_local=False)
        st = svc.connect("127.0.0.1:%d" % k.port, password="s3cret", wait=8)
        self.assertEqual(st["state"], "connected")
        self.assertIsNone(st["session"]["limit_s"])            # no limit with a password
        self.assertEqual(st["session"]["reset_interval_s"], 120 * 60)
        for n in (1, 2):                                       # still going after 2 resets (4 h)
            self.clock.advance(120 * 60)
            svc.tick()                                         # -> reset
            self.assertFalse(svc.radio.connected)
            self.clock.advance(15)
            svc.tick()                                         # -> reconnect
            self.assertTrue(wait_for(lambda: svc.radio.connected))
            self.assertEqual(svc.session_info()["resets"], n)
        self.assertIsNone(svc.session_info()["ended"])
        self.assertEqual(k.connections, 3)

    def test_bad_password_fails(self):
        k = self.kiwi(password="s3cret")
        svc = self.service()
        st = svc.connect("127.0.0.1:%d" % k.port, password="wrong", wait=8)
        self.assertEqual(st["state"], "failed")
        self.assertIn("password", st["last_error"].lower())

    def test_override_beats_exemption(self):
        k = self.kiwi()
        svc = self.service()
        st = svc.connect("127.0.0.1:%d" % k.port, wait=8, session_limit_min=10, reset_interval_min=0)
        self.assertEqual(st["session"]["limit_s"], 600)
        self.assertIsNone(st["session"]["reset_interval_s"])

    def test_periodic_reset_reconnects_and_pads_recording(self):
        k = self.kiwi()
        svc = self.service(exempt_local=False, limit_without_password_min=0,
                           reset_interval_min=1, reset_pause_s=15)
        svc.connect("127.0.0.1:%d" % k.port, wait=8)
        svc.start_recording(name="reset")
        time.sleep(1.0)
        self.clock.advance(61)
        svc.tick()                                       # -> disconnect, pause
        self.assertFalse(svc.radio.connected)
        self.assertIn("reset_in_progress_s", svc.session_info())
        self.assertTrue(svc.recording_info()["active"])  # recording survives the reset
        time.sleep(1.5)                                  # real gap the WAV must cover
        self.clock.advance(14)
        svc.tick()
        self.assertEqual(k.connections, 1)               # still pausing
        self.clock.advance(1)
        svc.tick()                                       # -> reconnect
        self.assertTrue(wait_for(lambda: svc.radio.connected))
        self.assertEqual(k.connections, 2)
        self.assertEqual(svc.session_info()["resets"], 1)
        time.sleep(1.0)
        info = svc.stop_recording()
        self.assertGreaterEqual(info["gap_seconds"], 1.4)
        with wave.open(info["path"]) as w:
            dur = w.getnframes() / w.getframerate()
        self.assertGreater(dur, 3.0)                     # audio + inserted silence

    def test_recording_max_seconds(self):
        k = self.kiwi()
        svc = self.service()
        svc.connect("127.0.0.1:%d" % k.port, wait=8)
        svc.start_recording(name="short", max_seconds=30)
        self.clock.advance(29)
        svc.tick()
        self.assertTrue(svc.recording_info()["active"])
        self.clock.advance(2)
        svc.tick()
        self.assertFalse(svc.recording_info()["active"])
        self.assertTrue(svc.radio.connected)             # connection stays up

    def test_recording_names(self):
        svc = self.service()
        svc.tune(14_074_000, "USB")
        p = svc.recording_path_for(name="{date}_{freq_khz}_{mode}/../evil")
        self.assertEqual(os.path.dirname(p), self.tmp)
        self.assertTrue(p.endswith("_14074.000_USB_.._evil.wav"), p)   # '/' neutralised
        open(p, "w").close()
        p2 = svc.recording_path_for(name="{date}_{freq_khz}_{mode}/../evil")
        self.assertTrue(p2.endswith("_1.wav"))           # never overwrites
        self.assertTrue(svc.recording_path_for(path=os.path.join(self.tmp, "x")).endswith("x.wav"))
        with self.assertRaises(ApiError):
            svc.recording_path_for(path=os.path.join(self.tmp, "x.mp3"))
        with self.assertRaises(ApiError):
            svc.recording_path_for(name="{nope}")

    def test_disconnect_stops_recording(self):
        k = self.kiwi()
        svc = self.service()
        svc.connect("127.0.0.1:%d" % k.port, wait=8)
        svc.start_recording(name="d")
        st = svc.disconnect()
        self.assertEqual(st["recording_stopped"]["reason"], "disconnect")
        self.assertEqual(st["state"], "idle")


class ApiCase(ServiceCase):
    token = None

    def setUp(self):
        super().setUp()
        self.service()
        self.api = ApiServer(self.svc, "127.0.0.1", 0, self.token)
        self.api.start()

    def tearDown(self):
        self.api.stop()
        super().tearDown()

    def call(self, method, path, body=None, headers=None, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.api.port, timeout=30)
        h = {"Content-Type": "application/json"} if body is not None or raw is not None else {}
        h.update(headers or {})
        data = raw if raw is not None else (json.dumps(body) if body is not None else None)
        c.request(method, "/api/v1" + path, body=data, headers=h)
        r = c.getresponse()
        out = r.status, json.loads(r.read())
        c.close()
        return out


class TestApi(ApiCase):
    def test_info_and_status(self):
        code, r = self.call("GET", "")
        self.assertEqual((code, r["ok"], r["api_version"]), (200, True, 1))
        self.assertIn("POST /api/v1/connect", r["endpoints"])
        code, r = self.call("GET", "/status")
        self.assertEqual(r["state"], "idle")
        self.assertFalse(r["recording"]["active"])

    def test_full_session(self):
        k = self.kiwi()
        code, r = self.call("POST", "/connect", {"server": "127.0.0.1:%d" % k.port, "wait": 8})
        self.assertEqual(code, 200, r)
        self.assertTrue(r["connected"])
        code, r = self.call("POST", "/tune", {"freq_khz": 7110, "mode": "LSB"})
        self.assertEqual(r["tune"], {"freq_hz": 7110000, "mode": "LSB", "passband": 2800})
        self.assertTrue(wait_for(lambda: any("lsb" in m and "freq=7110.000" in m for m in k.mods())))
        code, r = self.call("POST", "/record/start", {"name": "{date}_net", "max_seconds": 3600})
        self.assertEqual(code, 200, r)
        path = r["recording"]["path"]
        self.assertEqual(r["recording"]["max_seconds"], 3600)
        code, r = self.call("POST", "/record/start", {"name": "again"})
        self.assertEqual((code, r["error"]["code"]), (409, "conflict"))
        time.sleep(1.0)
        code, r = self.call("POST", "/record/stop", {})
        self.assertTrue(r["was_recording"])
        self.assertEqual(r["recording"]["path"], path)
        code, r = self.call("POST", "/disconnect", {})
        self.assertEqual(r["state"], "idle")
        with wave.open(path) as w:
            self.assertGreater(w.getnframes(), 6000)

    def test_connect_errors(self):
        k = self.kiwi(busy=True)
        code, r = self.call("POST", "/connect", {"server": "127.0.0.1:%d" % k.port, "wait": 8})
        self.assertEqual((code, r["error"]["code"]), (503, "busy"))
        k2 = self.kiwi(password="pw")
        code, r = self.call("POST", "/connect", {"server": "127.0.0.1:%d" % k2.port, "password": "x", "wait": 8})
        self.assertEqual((code, r["error"]["code"]), (502, "connect_failed"))
        code, r = self.call("POST", "/connect", {"server": "127.0.0.1:%d" % free_port()})
        self.assertEqual(code, 202)                      # no wait: accepted, worker keeps trying
        code, r = self.call("POST", "/connect", {})
        self.assertEqual((code, r["error"]["code"]), (400, "bad_request"))

    def test_validation(self):
        self.assertEqual(self.call("POST", "/tune", {"mode": "CW"})[0], 400)
        self.assertEqual(self.call("POST", "/tune", {"freq_khz": "7074"})[0], 400)
        self.assertEqual(self.call("POST", "/tune", {})[0], 400)
        self.assertEqual(self.call("POST", "/record/start", {"path": "/tmp/x.mp3"})[0], 400)
        self.assertEqual(self.call("POST", "/record/start", {"max_seconds": -5})[0], 400)
        self.assertEqual(self.call("POST", "/connect", raw="{not json")[1]["error"]["code"], "bad_json")
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("GET", "/connect")[0], 405)

    def test_browser_protections(self):
        # text/plain is what a cross-site <form> or no-preflight fetch can send
        code, r = self.call("POST", "/disconnect", raw="{}", headers={"Content-Type": "text/plain"})
        self.assertEqual((code, r["error"]["code"]), (415, "unsupported_media_type"))
        # DNS rebinding: evil.example resolving to 127.0.0.1
        code, r = self.call("GET", "/status", headers={"Host": "evil.example:7373"})
        self.assertEqual((code, r["error"]["code"]), (403, "forbidden_host"))
        self.assertEqual(self.call("GET", "/status", headers={"Host": "localhost:7373"})[0], 200)
        self.assertEqual(self.call("GET", "/status", headers={"Host": "[::1]:7373"})[0], 200)
        self.assertEqual(self.call("OPTIONS", "/connect")[0], 405)

    def test_ctl_client(self):
        self.assertEqual(ctl.main(["--port", str(self.api.port), "status"]), 0)
        self.assertEqual(ctl.main(["--port", str(self.api.port), "tune", "--khz", "3573", "--mode", "usb"]), 0)
        self.assertEqual(self.svc.radio.tune.freq_hz, 3573000)
        self.assertEqual(ctl.main(["--port", str(self.api.port), "tune", "--mode", "usb", "--passband", "5"]), 0)
        self.assertEqual(ctl.main(["--port", str(self.api.port), "record-stop"]), 0)
        self.assertEqual(ctl.main(["--port", str(free_port()), "status"]), 2)


class TestApiToken(ApiCase):
    token = "t0ken"

    def test_token_required(self):
        code, r = self.call("GET", "/status")
        self.assertEqual((code, r["error"]["code"]), (401, "unauthorized"))
        self.assertEqual(self.call("GET", "/status", headers={"Authorization": "Bearer t0ken"})[0], 200)
        self.assertEqual(self.call("GET", "/status", headers={"X-Api-Token": "t0ken"})[0], 200)
        self.assertEqual(ctl.main(["--port", str(self.api.port), "--token", "t0ken", "status"]), 0)
        self.assertEqual(ctl.main(["--port", str(self.api.port), "status"]), 1)

    def test_non_loopback_needs_token(self):
        with self.assertRaises(ValueError):
            ApiServer(self.svc, "0.0.0.0", 0, None)


class TestHeadless(unittest.TestCase):
    def test_headless_records_and_shuts_down_cleanly(self):
        kiwi = FakeKiwi()
        tmp = tempfile.mkdtemp()
        port = free_port()
        env = dict(os.environ, PYTHONPATH=ROOT)
        env.pop("KIWIREMOTE_API_TOKEN", None)
        win = sys.platform == "win32"
        proc = subprocess.Popen([sys.executable, "-m", "kiwiremote", "--headless", "--api-port", str(port),
                                 "--no-rigctl", "--record-dir", tmp, "--no-compression"],
                                cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if win else 0)
        try:
            if not wait_for(lambda: proc.poll() is not None or ctl.main(["--port", str(port), "info"]) == 0, 30) \
                    or proc.poll() is not None:
                if proc.poll() is None:
                    proc.kill()
                self.fail("headless API never came up (exit %s):\n%s"
                          % (proc.poll(), proc.communicate(timeout=10)[0].decode(errors="replace")))
            cl = ctl.ApiClient(port=port)
            code, r = cl.connect("127.0.0.1:%d" % kiwi.port, wait=8)
            self.assertEqual(code, 200, r)
            code, r = cl.record_start(name="headless")
            path = r["recording"]["path"]
            time.sleep(1.5)
            # graceful stop: SIGTERM, or Ctrl+Break on Windows (terminate() would kill it outright)
            proc.send_signal(signal.CTRL_BREAK_EVENT if win else signal.SIGTERM)
            out = proc.communicate(timeout=20)[0].decode(errors="replace")
            self.assertEqual(proc.returncode, 0, out)
            self.assertNotIn("PySide6", out)
        finally:
            if proc.poll() is None:
                proc.kill()
            kiwi.stop()
        with wave.open(path) as w:                       # header was finalized on SIGTERM
            self.assertGreater(w.getnframes(), 12000)


if __name__ == "__main__":
    unittest.main()
