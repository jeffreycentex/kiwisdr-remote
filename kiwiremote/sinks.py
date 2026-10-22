"""Audio sinks. Each sink receives mono int16 blocks at the Kiwi's sample rate.

All sinks share this interface (called from the Kiwi worker thread):
    set_input_rate(rate_hz)   # called on (re)connect, before any write
    write(samples_int16)
    close()

SoundCardSink   -> any output device (virtual cable, BlackHole, PulseAudio/PipeWire sink, speakers)
WavSink         -> 16-bit PCM WAV at the Kiwi's native rate
M4aSink         -> AAC in an MP4 container (.m4a / .mp4) via PyAV (optional dependency)
"""

import collections
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import wave
from fractions import Fraction

import numpy as np

from .resample import StreamResampler

log = logging.getLogger(__name__)

try:
    import sounddevice as sd
    HAVE_SOUNDDEVICE = True
    _SD_ERROR = None
except Exception as e:  # PortAudio missing on Linux raises OSError
    sd = None
    HAVE_SOUNDDEVICE = False
    _SD_ERROR = str(e)

try:
    import av
    HAVE_PYAV = True
except Exception:
    av = None
    HAVE_PYAV = False


# --------------------------------------------------------------------------
# Output-device enumeration
# --------------------------------------------------------------------------

def sounddevice_error():
    return _SD_ERROR


PULSE_PREFIX = "pulse-sink:"


def list_pulse_sinks():
    """Linux only: names of PulseAudio / PipeWire-pulse sinks (via pactl)."""
    if not sys.platform.startswith("linux") or not shutil.which("pactl"):
        return []
    try:
        res = subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True,
                             text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return []
    names = []
    for line in res.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) > 1:
            names.append(cols[1])
    return names


def create_null_sink(name="KiwiSDR"):
    """Linux only: create a virtual sink. Its 'Monitor of <name>' source is the
    'other end of the cable' that WSJT-X/fldigi record from. Lasts until the
    sound server restarts (see README for a permanent setup)."""
    if name in list_pulse_sinks():
        return name
    if not shutil.which("pactl"):
        raise RuntimeError("pactl not found (install pulseaudio-utils / pipewire-pulse)")
    res = subprocess.run(["pactl", "load-module", "module-null-sink", "sink_name=%s" % name,
                          "sink_properties=device.description=%s" % name],
                         capture_output=True, text=True, timeout=5)
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or "pactl load-module failed")
    return name


def _pulse_pa_index():
    """PortAudio device that routes into the pulse server (ALSA 'pulse' plugin)."""
    if not HAVE_SOUNDDEVICE:
        return None
    devs = sd.query_devices()
    for want in ("pulse", "pipewire", "default"):
        for idx, dev in enumerate(devs):
            if dev["name"] == want and dev["max_output_channels"] > 0:
                return idx
    return None


def list_output_devices():
    """Return [(key, label)] for every output-capable device.

    key is a stable string 'device name|host api' (indexes change between
    runs when devices are plugged/unplugged, names don't). On Linux, each
    PulseAudio/PipeWire sink is also listed individually.
    """
    if not HAVE_SOUNDDEVICE:
        return []
    out = []
    if _pulse_pa_index() is not None:
        for name in list_pulse_sinks():
            out.append((PULSE_PREFIX + name, "PulseAudio/PipeWire sink: %s" % name))
    apis = sd.query_hostapis()
    for dev in sd.query_devices():
        if dev["max_output_channels"] < 1:
            continue
        api = apis[dev["hostapi"]]["name"]
        key = "%s|%s" % (dev["name"], api)
        label = "%s  [%s]" % (dev["name"], api)
        out.append((key, label))
    return out


def resolve_device(key):
    """Map a stable key back to the current PortAudio device index (or None)."""
    if not HAVE_SOUNDDEVICE or not key:
        return None
    if key.startswith(PULSE_PREFIX):
        return _pulse_pa_index()
    name, _, api = key.partition("|")
    apis = sd.query_hostapis()
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] < 1:
            continue
        if dev["name"] == name and apis[dev["hostapi"]]["name"] == api:
            return idx
    return None


def guess_virtual_cable(devices):
    """Pick the most likely virtual-audio device key from list_output_devices()."""
    needles = ("cable input", "vb-audio", "blackhole", "kiwi", "loopback", "virtual", "null")
    for key, label in devices:
        low = label.lower()
        if any(n in low for n in needles):
            return key
    return None


# --------------------------------------------------------------------------
# Ring buffer (float32, single producer / single consumer, lock-protected)
# --------------------------------------------------------------------------

class _Ring:
    def __init__(self, capacity):
        self._buf = np.zeros(capacity, dtype=np.float32)
        self._cap = capacity
        self._r = 0
        self._n = 0
        self._lock = threading.Lock()

    def __len__(self):
        return self._n

    def push(self, x):
        with self._lock:
            n = len(x)
            if n >= self._cap:            # absurdly large block: keep the tail
                x = x[-self._cap:]
                n = len(x)
            overflow = self._n + n - self._cap
            if overflow > 0:              # drop oldest
                self._r = (self._r + overflow) % self._cap
                self._n -= overflow
            w = (self._r + self._n) % self._cap
            first = min(n, self._cap - w)
            self._buf[w:w + first] = x[:first]
            self._buf[:n - first] = x[first:]
            self._n += n

    def pull(self, out):
        """Fill out[] as far as possible; return number of samples written."""
        with self._lock:
            n = min(len(out), self._n)
            first = min(n, self._cap - self._r)
            out[:first] = self._buf[self._r:self._r + first]
            out[first:n] = self._buf[:n - first]
            self._r = (self._r + n) % self._cap
            self._n -= n
            return n

    def discard_to(self, keep):
        with self._lock:
            if self._n > keep:
                drop = self._n - keep
                self._r = (self._r + drop) % self._cap
                self._n = keep

    def clear(self):
        with self._lock:
            self._r = self._n = 0


# --------------------------------------------------------------------------
# Sound card / virtual cable
# --------------------------------------------------------------------------

class SoundCardSink:
    """Plays Kiwi audio on an output device with clock-drift compensation.

    The Kiwi's sample clock and the sound card's clock never match exactly.
    A PI controller on the ring-buffer fill level trims the resampling ratio
    by at most +/-0.5 % so the buffer stays near the target latency instead
    of slowly overflowing or underrunning.
    """

    # Loop tuning. Fill error e = (fill - target)/target drives
    #   trim = 1 - KP*e - I,   dI/dt = KI*e
    # giving natural frequency ~0.012 rad/s and damping ~0.85: it settles in a
    # few minutes and network jitter moves the pitch by well under 0.1 %
    # (about 1 Hz at a 1500 Hz FT8 tone).
    TARGET_LATENCY = 0.20   # seconds left in the ring when an on-time packet arrives
    KP = 0.003
    KI = 2e-5               # per second
    FILL_TAU = 5.0          # seconds, smoothing of the measured fill level
    INTEG_BAND = 0.3        # integrate only while |e| is below this
    LATE_WINDOW = 20.0      # seconds; earliest packet in this window = 'on time'
    LATE_RESET = 2.0        # a packet this late starts a new timeline
    TRIM_LIMIT = 0.005
    # The callback runs Python, so it must not be scheduled every 1-2 ms:
    # 'latency="low"' gives 64-frame buffers (1.3 ms on BlackHole), any GIL
    # stall then drops device IO cycles, the ring overfills and the trim
    # winds up to its limit. ~20 ms blocks with 100 ms of device latency
    # leave plenty of slack.
    BLOCK_SECONDS = 0.02
    DEVICE_LATENCY = 0.10
    FADE_SECONDS = 0.005    # ramp at underrun / resume, avoids clicks
    LIMIT_KNEE = 0.9        # soft limiter above this level (gain > 100 %)

    def __init__(self, device_key, gain=1.0):
        if not HAVE_SOUNDDEVICE:
            raise RuntimeError("sounddevice/PortAudio not available: %s" % _SD_ERROR)
        self.device_key = device_key
        self.gain = float(gain)
        self._stream = None
        self._resampler = None
        self._ring = None
        self._out_rate = None
        self._channels = 1
        self._primed = False
        self._fill_ema = 0.0
        self._integ = 0.0
        self._t_in = 0.0                 # Kiwi stream position, seconds
        self._late_min = collections.deque()
        self._clock = time.monotonic     # replaceable for simulated-time tests
        self.underruns = 0

    # -- worker-thread side --------------------------------------------------

    def set_input_rate(self, rate):
        idx = resolve_device(self.device_key)
        if idx is None:
            raise RuntimeError("audio device not found: %s" % self.device_key.split("|")[0])
        if self.device_key.startswith(PULSE_PREFIX):
            # libpulse routes a new playback stream to $PULSE_SINK when set
            os.environ["PULSE_SINK"] = self.device_key[len(PULSE_PREFIX):]
        else:
            os.environ.pop("PULSE_SINK", None)
        info = sd.query_devices(idx)
        out_rate = int(info["default_samplerate"]) or 48000
        channels = max(1, min(2, int(info["max_output_channels"])))
        if self._stream is not None and out_rate == self._out_rate and self._resampler \
                and abs(self._resampler.in_rate - rate) < 1e-6:
            return
        self._close_stream()
        self._out_rate = out_rate
        self._channels = channels
        self._resampler = StreamResampler(rate, out_rate)
        self._ring = _Ring(int(out_rate * 4))
        self._primed = False
        self._fill_ema = 0.0
        self._integ = 0.0
        self._t_in = 0.0
        self._late_min.clear()
        self._fade = np.linspace(0.0, 1.0, max(2, int(out_rate * self.FADE_SECONDS)),
                                 dtype=np.float32)
        self._fade_in = True
        self._stream = sd.OutputStream(
            device=idx, samplerate=out_rate, channels=channels, dtype="float32",
            blocksize=int(out_rate * self.BLOCK_SECONDS),
            latency=max(self.DEVICE_LATENCY, float(info.get("default_high_output_latency", 0.0))),
            callback=self._callback)
        self._stream.start()
        log.info("sound card '%s' opened at %d Hz, %d ch, block %d, latency %.0f ms, resampler=%s",
                 info["name"], out_rate, channels, self._stream.blocksize,
                 1000.0 * self._stream.latency, self._resampler.backend)

    def write(self, samples):
        if self._stream is None:
            return
        x = samples.astype(np.float32) * (self.gain / 32768.0)
        rate = self._out_rate
        target = self.TARGET_LATENCY * rate
        dt = len(samples) / self._resampler.in_rate
        fill = len(self._ring)               # trough: just before the refill
        if fill > 4 * target:                # way behind (e.g. device stalled) - catch up
            self._ring.discard_to(int(target))
            fill = len(self._ring)
        # Kiwi packets carry up to 170 ms (2048 samples when compressed) and
        # arrive with network jitter, so the raw trough jumps around by the
        # packet's lateness. Correct for it: lateness = arrival time minus the
        # packet's place in the Kiwi sample stream, relative to the earliest
        # packet of the last few seconds. 'level' is then the trough an
        # on-time packet would see: jitter drops out, clock drift does not.
        late = self._lateness(dt)
        level = fill + late * rate
        if not self._primed:
            # (Re)start so that the next on-time packet finds exactly 'target'
            # left: the loop starts with zero error. After a stall the late
            # packets arrive in a bunch; their backlog is dropped here (silence
            # already took its place) instead of being a step that the loop
            # must drain, which wound up the integrator and cascaded (issue #6).
            y = self._limit(self._resampler.process(x, 1.0 - self._integ))
            self._ring.push(y)
            if level >= target:
                keep = int(target + len(y) - late * rate)
                if keep > 0:
                    self._ring.discard_to(keep)
                    self._fill_ema = target
                    self._primed = True
                else:
                    self._ring.clear()
            return
        a = min(1.0, dt / self.FILL_TAU)
        self._fill_ema += a * (level - self._fill_ema)
        err = (self._fill_ema - target) / target
        # Only clock drift belongs in the integrator, and drift moves the level
        # slowly; the proportional term handles anything sudden.
        if abs(err) < self.INTEG_BAND:
            self._integ = float(np.clip(self._integ + self.KI * err * dt,
                                        -self.TRIM_LIMIT, self.TRIM_LIMIT))
        trim = float(np.clip(1.0 - self.KP * err - self._integ,
                             1.0 - 2 * self.TRIM_LIMIT, 1.0 + 2 * self.TRIM_LIMIT))
        self._ring.push(self._limit(self._resampler.process(x, trim)))

    def _lateness(self, dt):
        """Seconds this packet is later than the earliest one in LATE_WINDOW."""
        now = self._clock()
        late = now - self._t_in
        self._t_in += dt
        q = self._late_min                  # (time, lateness), lateness increasing
        if q and late - q[0][1] > self.LATE_RESET:
            q.clear()                       # long gap (reconnect, sleep): new timeline
        while q and q[-1][1] >= late:
            q.pop()
        q.append((now, late))
        while q[0][0] < now - self.LATE_WINDOW:
            q.popleft()
        return late - q[0][1]

    def _limit(self, y):
        """Soft knee instead of hard clipping at full scale (gain > 100 %, sinc overshoot)."""
        a = np.abs(y)
        over = a > self.LIMIT_KNEE
        if over.any():
            head = 1.0 - self.LIMIT_KNEE
            y = y.copy()
            y[over] = np.sign(y[over]) * (self.LIMIT_KNEE + head * np.tanh((a[over] - self.LIMIT_KNEE) / head))
        return y

    def close(self):
        self._close_stream()

    def stats(self):
        if self._ring is None or not self._out_rate:
            return {}
        return {"buffer_ms": 1000.0 * len(self._ring) / self._out_rate,
                "trim_ppm": -1e6 * self._integ, "underruns": self.underruns}

    # -- PortAudio-thread side -----------------------------------------------

    def _callback(self, outdata, frames, time_info, status):
        mono = np.zeros(frames, dtype=np.float32)
        if self._primed:
            got = self._ring.pull(mono)
            if self._fade_in:
                n = min(got, len(self._fade))
                mono[:n] *= self._fade[:n]
                self._fade_in = False
            if got < frames:
                self.underruns += 1
                n = min(got, len(self._fade))
                mono[got - n:got] *= self._fade[::-1][len(self._fade) - n:]
                self._primed = False     # re-prime before resuming, avoids stutter
                self._fade_in = True
        outdata[:] = mono[:, None] if self._channels > 1 else mono.reshape(-1, 1)

    def _close_stream(self):
        s, self._stream = self._stream, None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception as e:
                log.debug("closing sound stream: %s", e)


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def _numbered(path, n):
    if n == 0:
        return path
    root, ext = os.path.splitext(path)
    return "%s_%d%s" % (root, n, ext)


class WavSink:
    """16-bit mono PCM at the Kiwi's native rate (typically ~12 kHz).

    If the sample rate changes mid-recording (connected to a different
    server), the current file is closed and a new one '<name>_N.wav' opened.
    """

    def __init__(self, path):
        self.path = path
        self._wav = None
        self._rate = None
        self._part = 0
        self.frames = 0
        self.current_path = None

    def set_input_rate(self, rate):
        rate = int(round(rate))
        if self._wav is not None and rate == self._rate:
            return
        if self._wav is not None:
            self._wav.close()
            self._part += 1
        self._rate = rate
        self.current_path = _numbered(self.path, self._part)
        self._wav = wave.open(self.current_path, "wb")
        self._wav.setnchannels(1)
        self._wav.setsampwidth(2)
        self._wav.setframerate(rate)
        log.info("recording WAV %s at %d Hz", self.current_path, rate)

    def write(self, samples):
        if self._wav is None:
            return
        self._wav.writeframesraw(samples.astype("<i2", copy=False).tobytes())
        self.frames += len(samples)

    def seconds(self):
        return self.frames / self._rate if self._rate else 0.0

    def close(self):
        if self._wav is not None:
            self._wav.close()   # patches the RIFF header sizes
            self._wav = None


class M4aSink:
    """AAC audio in an MP4 container (.m4a or .mp4). Requires PyAV."""

    OUT_RATE = 48000
    BITRATE = 64000

    def __init__(self, path):
        if not HAVE_PYAV:
            raise RuntimeError("MP4/M4A recording needs PyAV: pip install av")
        self.path = path
        self._container = None
        self._stream = None
        self._resampler = None
        self._in_rate = None
        self._pts = 0
        self._part = 0
        self.frames = 0
        self.current_path = None

    def set_input_rate(self, rate):
        rate = int(round(rate))
        if self._container is not None and rate == self._in_rate:
            return
        if self._container is not None:
            self._finish()
            self._part += 1
        self._in_rate = rate
        self._pts = 0
        self.current_path = _numbered(self.path, self._part)
        self._container = av.open(self.current_path, mode="w", format="mp4")
        self._stream = self._container.add_stream("aac", rate=self.OUT_RATE)
        cc = self._stream.codec_context
        try:
            cc.layout = "mono"
        except Exception:
            pass
        cc.bit_rate = self.BITRATE
        self._resampler = av.AudioResampler(format="fltp", layout="mono", rate=self.OUT_RATE)
        log.info("recording AAC/MP4 %s (%d Hz in, %d Hz out)", self.current_path, rate, self.OUT_RATE)

    def write(self, samples):
        if self._container is None:
            return
        arr = np.ascontiguousarray(samples.astype(np.int16, copy=False)).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(arr, format="s16", layout="mono")
        frame.sample_rate = self._in_rate
        frame.pts = self._pts
        frame.time_base = Fraction(1, self._in_rate)
        self._pts += arr.shape[1]
        self.frames += arr.shape[1]
        for f in self._resampler.resample(frame):
            for packet in self._stream.encode(f):
                self._container.mux(packet)

    def seconds(self):
        return self.frames / self._in_rate if self._in_rate else 0.0

    def _finish(self):
        try:
            for f in self._resampler.resample(None):
                for packet in self._stream.encode(f):
                    self._container.mux(packet)
            for packet in self._stream.encode(None):
                self._container.mux(packet)
        finally:
            self._container.close()
            self._container = None

    def close(self):
        if self._container is not None:
            self._finish()


def make_file_sink(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".wav":
        return WavSink(path)
    if ext in (".m4a", ".mp4", ".aac"):
        return M4aSink(path if ext != ".aac" else os.path.splitext(path)[0] + ".m4a")
    raise ValueError("unsupported file type %r (use .wav, .m4a or .mp4)" % ext)


def default_recording_name(freq_hz, mode, ext):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return "kiwi_%s_%.3fkHz_%s%s" % (stamp, freq_hz / 1000.0, mode, ext)
