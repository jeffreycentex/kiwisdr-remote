"""Simulated-time test of SoundCardSink's drift compensation (no audio hardware).

The Kiwi produces 2048-sample packets (as compressed audio does) at 12001.135 Hz
with random network delay; the 'sound card' consumes bursts of blocksize-frame
callbacks at 48000 Hz * (1 + clock error). There must be no underruns, the
ring's trough must sit at its target, and the trim must hold the true clock
offset without wandering.
"""

import os
import sys
import types
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kiwiremote import sinks  # noqa: E402


class _FakeStream:
    def __init__(self, **kw):
        self.kw = kw
        self.blocksize = kw.get("blocksize", 0)
        self.latency = kw.get("latency", 0.0)

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass


def _install_fake_sd():
    fake = types.SimpleNamespace(
        query_devices=lambda idx=None: {"name": "Fake Cable", "default_samplerate": 48000.0,
                                        "max_output_channels": 2, "hostapi": 0}
        if idx is not None else [{"name": "Fake Cable", "default_samplerate": 48000.0,
                                  "max_output_channels": 2, "hostapi": 0}],
        query_hostapis=lambda: [{"name": "FakeAPI"}],
        OutputStream=_FakeStream,
    )
    sinks.sd = fake
    sinks.HAVE_SOUNDDEVICE = True


class TestDrift(unittest.TestCase):
    def run_sim(self, ppm, seconds=900, blk=2048, burst=4, jitter=0.03, seed=1):
        """Kiwi packets of blk samples arrive with random network delay (mean
        `jitter` s, in order); the card pulls `burst` callbacks back to back
        (BlackHole/CoreAudio deliver 4 x 20 ms at once)."""
        _install_fake_sd()
        rng = np.random.default_rng(seed)
        sink = sinks.SoundCardSink("Fake Cable|FakeAPI")
        in_rate = 12001.135
        sink.set_input_rate(in_rate)
        sink._clock = lambda: arrive
        out_rate, frames = 48000 * (1 + ppm * 1e-6), sink._stream.blocksize
        out = np.zeros((frames, 2), dtype=np.float32)
        k = 0
        arrive = 0.0
        t_out = 0.0
        troughs, trims = [], []
        while arrive < seconds:
            nominal = (k + 1) * blk / in_rate
            arrive = max(arrive, nominal + min(rng.exponential(jitter), 4 * jitter))
            while t_out < arrive:
                for _ in range(burst):
                    sink._callback(out, frames, None, None)
                t_out += burst * frames / out_rate
            if arrive > seconds / 2:
                troughs.append(len(sink._ring) / 48000)
                trims.append(sink.stats()["trim_ppm"])
            n = np.arange(k * blk, (k + 1) * blk)
            sink.write((8000 * np.sin(2 * np.pi * 1000 * n / in_rate)).astype(np.int16))
            k += 1
        return sink, np.array(troughs), np.array(trims)

    def check(self, ppm, **kw):
        sink, troughs, trims = self.run_sim(ppm, **kw)
        self.assertEqual(sink.underruns, 0)
        self.assertGreater(troughs.min(), 0.0)
        self.assertLess(abs(np.mean(troughs) - sink.TARGET_LATENCY), 0.05, np.mean(troughs))
        # settled on the true offset, no wandering (issue #6: it ran away by 1000+ ppm)
        self.assertLess(np.max(np.abs(trims - ppm)), 30, (trims.min(), trims.max()))

    def test_fast_card(self):
        self.check(+300)

    def test_slow_card(self):
        self.check(-300)

    def test_matched_clocks_bursty(self):
        self.check(0, burst=5)

    def test_small_packets(self):
        self.check(+100, blk=512, burst=1)

    def test_stalls_do_not_cascade(self):
        # network stalls longer than the buffer: underruns are unavoidable,
        # but each restart must not push the trim further (it used to run to
        # the limit). Far harsher than a real link: an underrun every ~10 s.
        sink, troughs, trims = self.run_sim(0, jitter=0.05, burst=5)
        self.assertGreater(sink.underruns, 0)
        self.assertLess(np.max(np.abs(trims)), 1500, (trims.min(), trims.max()))

    def test_requests_large_blocks(self):
        # 64-frame 'low latency' callbacks starve under GIL load (issue #6)
        _install_fake_sd()
        sink = sinks.SoundCardSink("Fake Cable|FakeAPI")
        sink.set_input_rate(12000)
        self.assertGreaterEqual(sink._stream.kw["blocksize"], 512)
        self.assertGreaterEqual(sink._stream.kw["latency"], 0.05)

    def test_soft_limiter(self):
        _install_fake_sd()
        sink = sinks.SoundCardSink("Fake Cable|FakeAPI", gain=2.0)
        y = sink._limit(np.array([0.5, -0.8, 0.95, -1.5, 3.0], dtype=np.float32))
        self.assertTrue(np.all(np.abs(y) <= 1.0))
        self.assertEqual(list(y[:2]), [0.5, np.float32(-0.8)])
        self.assertTrue(-1.0 < y[3] < -0.9 and 0.9 < y[4] <= 1.0)

    def test_underrun_fades_out(self):
        _install_fake_sd()
        sink = sinks.SoundCardSink("Fake Cable|FakeAPI")
        sink.set_input_rate(12000)
        sink._ring.push(np.full(1000, 0.5, dtype=np.float32))
        sink._primed = True
        out = np.zeros((2000, 2), dtype=np.float32)
        sink._callback(out, 2000, None, None)
        self.assertEqual(sink.underruns, 1)
        self.assertAlmostEqual(float(out[0, 0]), 0.0)          # faded in
        self.assertAlmostEqual(float(out[500, 0]), 0.5)
        self.assertAlmostEqual(float(out[999, 0]), 0.0)        # faded out, no step to silence


if __name__ == "__main__":
    unittest.main()
