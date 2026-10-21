"""Headless tests: resampler, rigctld protocol, and a full connect/record cycle
against tests/fake_kiwi.py.  Run:  python -m unittest discover -s tests -v
"""

import os
import socket
import sys
import tempfile
import time
import unittest
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fake_kiwi import FakeKiwi  # noqa: E402
from kiwiremote import resample  # noqa: E402
from kiwiremote.radio import RadioController  # noqa: E402
from kiwiremote.rigctld import RigctldServer  # noqa: E402
from kiwiremote.state import ServerSpec, passband_edges  # noqa: E402


def wait_for(pred, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


class TestState(unittest.TestCase):
    def test_server_parse(self):
        self.assertEqual(ServerSpec.parse("http://kiwi.example.net:8074/").label(), "kiwi.example.net:8074")
        self.assertEqual(ServerSpec.parse("10.0.0.5").port, 8073)
        self.assertEqual(ServerSpec.parse("[::1]:8075").host, "::1")

    def test_passband(self):
        self.assertEqual(passband_edges("USB", 2800), (200, 3000))
        self.assertEqual(passband_edges("LSB", 2800), (-3000, -200))
        self.assertEqual(passband_edges("AM", 0), (-4900, 4900))


class TestResampler(unittest.TestCase):
    def _check(self, rs, in_rate, out_rate):
        n_blocks, blk = 200, 512
        t = np.arange(n_blocks * blk) / in_rate
        x = np.sin(2 * np.pi * 1000 * t).astype(np.float32)
        out = np.concatenate([rs.process(x[i:i + blk]) for i in range(0, len(x), blk)])
        expect = len(x) * out_rate / in_rate
        self.assertLess(abs(len(out) - expect), 0.01 * expect)
        # continuity: no sample-to-sample jump bigger than a 1 kHz sine allows at out_rate
        max_step = 2 * np.pi * 1000 / out_rate * 1.05
        self.assertLess(np.max(np.abs(np.diff(out[200:]))), max_step)

    def test_linear(self):
        rs = resample.StreamResampler(12001.135, 48000)
        rs._src = None
        self._check(rs, 12001.135, 48000)

    def test_trim_changes_length(self):
        rs = resample.StreamResampler(12000, 48000)
        rs._src = None
        x = np.zeros(12000, dtype=np.float32)
        a = sum(len(rs.process(x[i:i + 500], 1.004)) for i in range(0, 12000, 500))
        self.assertAlmostEqual(a / 48000, 1.004, delta=0.0005)




class TestRigctld(unittest.TestCase):
    def setUp(self):
        self.ctl = RadioController()
        self.srv = RigctldServer(self.ctl, "127.0.0.1", 0)
        self.srv.start()
        self.sock = socket.create_connection(("127.0.0.1", self.srv.port), timeout=3)
        self.f = self.sock.makefile("rwb")

    def tearDown(self):
        self.sock.close()
        self.srv.stop()

    def ask(self, line, lines=1):
        self.f.write((line + "\n").encode())
        self.f.flush()
        return [self.f.readline().decode().rstrip("\n") for _ in range(lines)]

    def test_freq_mode_roundtrip(self):
        self.assertEqual(self.ask("F 14074000"), ["RPRT 0"])
        self.assertEqual(self.ask("f"), ["14074000"])
        self.assertEqual(self.ask("M USB 2400"), ["RPRT 0"])
        self.assertEqual(self.ask("m", 2), ["USB", "2400"])
        self.assertEqual(self.ask("\\set_mode PKTLSB 0"), ["RPRT 0"])
        self.assertEqual(self.ask("m", 2), ["LSB", "2800"])
        self.assertEqual(self.ask("M CW 500"), ["RPRT -11"])
        self.assertEqual(self.ctl.tune.freq_hz, 14074000)

    def test_vfo_arg_and_extended(self):
        self.assertEqual(self.ask("F VFOA 7074000"), ["RPRT 0"])
        self.assertEqual(self.ask("+f", 3), ["get_freq:", "Frequency: 7074000", "RPRT 0"])
        self.assertEqual(self.ask("\\get_freq"), ["7074000"])

    def test_dump_state_and_misc(self):
        self.f.write(b"\\dump_state\n")
        self.f.flush()
        got = []
        while True:
            l = self.f.readline().decode().rstrip("\n")
            got.append(l)
            if l == "done":
                break
        self.assertEqual(got[1], "2")
        self.assertEqual(self.ask("\\chk_vfo"), ["CHKVFO 0"])
        self.assertEqual(self.ask("t"), ["0"])
        self.assertEqual(self.ask("T 1"), ["RPRT 0"])
        self.assertEqual(self.ask("v"), ["VFOA"])
        self.assertEqual(self.ask("l STRENGTH"), ["-54"])   # no signal yet: -127 dBm -> S9-54
        self.assertEqual(self.ask("F 99999999999"), ["RPRT -1"])

    def test_two_commands_one_line(self):
        self.assertEqual(self.ask("F 3573000 f", 2), ["RPRT 0", "3573000"])


class TestEndToEnd(unittest.TestCase):
    def test_connect_tune_record(self):
        kiwi = FakeKiwi()
        events = []
        ctl = RadioController(on_event=lambda k, p: events.append((k, p)), compression=False)
        rig = RigctldServer(ctl, "127.0.0.1", 0)
        rig.start()
        tmp = tempfile.mkdtemp()
        wav_path = os.path.join(tmp, "rec.wav")
        try:
            ctl.set_frequency(7_074_000)
            ctl.set_mode("USB")
            ctl.start_recording(wav_path)
            ctl.connect(ServerSpec("127.0.0.1", kiwi.port))
            self.assertTrue(wait_for(lambda: ctl.connected), "never connected")
            self.assertTrue(wait_for(lambda: kiwi.mods()))
            self.assertIn("SET mod=usb low_cut=200 high_cut=3000 freq=7074.000", kiwi.mods()[0])
            self.assertIn("SET compression=0", kiwi.commands)

            # retune through rigctl while streaming
            with socket.create_connection(("127.0.0.1", rig.port)) as s:
                s.sendall(b"F 14074000\nM LSB 2400\n")
                f = s.makefile("rb")
                self.assertEqual(f.readline(), b"RPRT 0\n")
                self.assertEqual(f.readline(), b"RPRT 0\n")
            self.assertTrue(wait_for(lambda: any("lsb" in m and "freq=14074.000" in m for m in kiwi.mods())),
                            kiwi.mods())
            self.assertTrue(any(k == "tune" and p[1] == "rigctl" for k, p in events))

            time.sleep(1.5)
            self.assertAlmostEqual(ctl.rssi, -80.0, delta=0.2)

            # server drops us -> auto reconnect
            kiwi.drop_clients()
            self.assertTrue(wait_for(lambda: kiwi.connections >= 2, 10), "no reconnect")
            self.assertTrue(wait_for(lambda: ctl.connected, 5))

            info = ctl.stop_recording()
            self.assertGreater(info["seconds"], 1.0)
        finally:
            ctl.shutdown()
            rig.stop()
            kiwi.stop()

        with wave.open(wav_path) as w:
            self.assertEqual(w.getframerate(), 12001)
            self.assertEqual(w.getnchannels(), 1)
            data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        self.assertGreater(len(data), 12000)
        # it's our 1 kHz tone at amplitude 8000
        self.assertAlmostEqual(np.max(np.abs(data[3000:12000])), 8000, delta=50)   # first packets are ADPCM, sent before SET compression=0 landed
        spec = np.abs(np.fft.rfft(data[:12000]))
        self.assertAlmostEqual(np.argmax(spec) * 12001 / 12000, 1000, delta=3)

    def test_compressed_audio(self):
        kiwi = FakeKiwi()
        ctl = RadioController(compression=True)
        path = os.path.join(tempfile.mkdtemp(), "c.wav")
        try:
            ctl.start_recording(path)
            ctl.connect(ServerSpec("127.0.0.1", kiwi.port))
            self.assertTrue(wait_for(lambda: ctl.connected))
            time.sleep(2.0)
            ctl.stop_recording()
        finally:
            ctl.shutdown()
            kiwi.stop()
        self.assertNotIn("SET compression=0", kiwi.commands)
        with wave.open(path) as w:
            data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(float)
        seg = data[2000:14000]
        spec = np.abs(np.fft.rfft(seg))
        self.assertAlmostEqual(np.argmax(spec) * 12001 / len(seg), 1000, delta=3)
        # ADPCM reconstruction of a clean tone should be within a few % RMS
        t = np.arange(len(seg))
        ref_amp = np.sqrt(2) * np.std(seg)
        self.assertAlmostEqual(ref_amp, 8000, delta=400)

    def test_busy_server_reports_status(self):
        kiwi = FakeKiwi(busy=True)
        events = []
        ctl = RadioController(on_event=lambda k, p: events.append((k, p)))
        try:
            ctl.connect(ServerSpec("127.0.0.1", kiwi.port))
            self.assertTrue(wait_for(lambda: any(k == "status" and "busy" in p[0] for k, p in events)))
        finally:
            ctl.shutdown()
            kiwi.stop()


if __name__ == "__main__":
    unittest.main()
