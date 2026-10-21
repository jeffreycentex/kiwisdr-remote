"""Simulated-time test of SoundCardSink's drift compensation (no audio hardware).

The Kiwi produces 512-sample blocks at 12001.135 Hz; the 'sound card' consumes
256-frame callbacks at 48000 Hz * (1 + clock error). After settling, the ring
buffer must sit near its 200 ms target with no underruns, for both a fast and
a slow sound-card clock.
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
    def run_sim(self, ppm, seconds=900):
        _install_fake_sd()
        sink = sinks.SoundCardSink("Fake Cable|FakeAPI")
        sink.set_input_rate(12001.135)
        in_rate, blk = 12001.135, 512
        out_rate, frames = 48000 * (1 + ppm * 1e-6), 256
        t_in = t_out = 0.0
        out = np.zeros((frames, 2), dtype=np.float32)
        fills = []
        underruns_after_settle = None
        tone_phase = 0
        while t_in < seconds:
            if t_in <= t_out:
                n = np.arange(tone_phase, tone_phase + blk)
                sink.write((8000 * np.sin(2 * np.pi * 1000 * n / in_rate)).astype(np.int16))
                tone_phase += blk
                t_in += blk / in_rate
            else:
                sink._callback(out, frames, None, None)
                t_out += frames / out_rate
            if t_in > seconds / 2:
                if underruns_after_settle is None:
                    underruns_after_settle = sink.underruns
                fills.append(len(sink._ring) / 48000)
        return sink, np.array(fills), sink.underruns - underruns_after_settle

    def test_fast_card(self):
        sink, fills, late_underruns = self.run_sim(+300)
        self.assertEqual(late_underruns, 0)
        self.assertLess(abs(np.mean(fills) - 0.2), 0.03, np.mean(fills))
        self.assertAlmostEqual(sink.stats()["trim_ppm"], 300, delta=25)

    def test_slow_card(self):
        sink, fills, late_underruns = self.run_sim(-300)
        self.assertEqual(late_underruns, 0)
        self.assertLess(abs(np.mean(fills) - 0.2), 0.03, np.mean(fills))
        self.assertAlmostEqual(sink.stats()["trim_ppm"], -300, delta=25)


if __name__ == "__main__":
    unittest.main()
