"""Streaming mono resampler with a run-time adjustable ratio.

Uses libsamplerate (python 'samplerate' package) when installed, otherwise a
stateful linear interpolator. Both keep phase continuity across blocks so
there are no clicks at block boundaries, and both accept a small ratio trim
each call (used for sound-card clock-drift compensation).
"""

import logging

import numpy as np

try:
    import samplerate as _src
    HAVE_LIBSAMPLERATE = True
except Exception:  # ImportError, or missing native lib (OSError)
    _src = None
    HAVE_LIBSAMPLERATE = False

log = logging.getLogger(__name__)


class StreamResampler:
    def __init__(self, in_rate, out_rate, quality="sinc_medium"):
        self.in_rate = float(in_rate)
        self.out_rate = float(out_rate)
        self._src = None
        if HAVE_LIBSAMPLERATE:
            try:
                self._src = _src.Resampler(quality, channels=1)
            except Exception as e:  # pragma: no cover - platform specific
                log.warning("libsamplerate unavailable (%s); using linear interpolation", e)
        # linear-interpolator state
        self._last = np.float32(0.0)
        self._pos = 1.0  # position of next output sample in [last, x0, x1, ...] coordinates

    @property
    def backend(self):
        return "libsamplerate" if self._src is not None else "linear"

    def process(self, x, trim=1.0):
        """x: 1-D float32 array at in_rate. trim: multiplies the output/input ratio."""
        x = np.asarray(x, dtype=np.float32)
        if x.size == 0:
            return x
        ratio = (self.out_rate / self.in_rate) * trim
        if self._src is not None:
            return self._src.process(x, ratio, end_of_input=False).astype(np.float32, copy=False)
        return self._linear(x, ratio)

    def _linear(self, x, ratio):
        buf = np.concatenate(([self._last], x))
        step = 1.0 / ratio                 # input samples per output sample
        end = len(buf) - 1                 # last index we can interpolate up to
        if self._pos > end:
            self._pos -= len(x)
            self._last = buf[-1]
            return np.zeros(0, dtype=np.float32)
        n = int(np.floor((end - self._pos) / step)) + 1
        pos = self._pos + step * np.arange(n)
        out = np.interp(pos, np.arange(len(buf)), buf).astype(np.float32)
        self._pos = pos[-1] + step - len(x)  # rebase so buf[0] of next call = this buf[-1]
        self._last = buf[-1]
        return out
