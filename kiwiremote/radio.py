# SPDX-License-Identifier: GPL-3.0-or-later
"""Kiwi connection worker + controller (no GUI dependencies).

Threading model
---------------
* GUI thread and rigctld thread call RadioController.set_frequency()/set_mode().
  These only update the shared TuneState under a lock and set a 'dirty' flag.
* One worker thread owns the websocket. Between incoming messages (audio
  arrives every ~40 ms) it applies the pending tune state with 'SET mod=...'.
  Only this thread ever touches the socket, so no locking is needed there.
* Audio blocks are handed to the sinks from the worker thread.
* Events (status, tune changes, S-meter, ...) go to a single callback; the GUI
  forwards them into Qt signals so they are delivered on the GUI thread.
"""

import logging
import re
import socket
import threading
import time
import types
from urllib.parse import urlparse

from . import kiwi_import as K
from .sinks import SoundCardSink, make_file_sink
from .state import (DEFAULT_MAX_FREQ_HZ, MIN_FREQ_HZ, MODES, ServerSpec, TuneState,
                    normalize_mode, passband_edges)

log = logging.getLogger(__name__)

try:
    from mod_pywebsocket._stream_base import ConnectionTerminatedException
except Exception:  # pragma: no cover
    class ConnectionTerminatedException(Exception):
        pass

# Connection states reported by RadioController.state
IDLE = "idle"                  # no worker running
CONNECTING = "connecting"      # first attempt in progress
CONNECTED = "connected"        # audio is flowing
RECONNECTING = "reconnecting"  # lost the link, retrying
BUSY = "busy"                  # server full, retrying
FAILED = "failed"              # gave up (bad password, server down, redirect loop)

RECONNECT_DELAY = 5.0
MAX_GAP_FILL = 1800.0          # seconds of silence inserted at most for one gap
BUSY_DELAY = 15.0
SMETER_INTERVAL = 0.1
MAX_HTTP_REDIRECTS = 5


class KiwiRedirectLoopError(K.KiwiDownError):
    """HTTP redirects went in a circle or never ended, e.g. proxy.kiwisdr.com and
    proxy2.kiwisdr.com bouncing 307s between each other while the Kiwi is offline."""


def _make_options(server, tune, compression):
    """Build the options object kiwiclient's KiwiSDRStream reads attributes from."""
    return types.SimpleNamespace(
        server_host=server.host, server_port=server.port,
        password=server.password, tlimit_password="", user=server.user,
        admin=False, nolocal=False, no_api=False, bad_cmd=False,
        modulation=MODES[tune.mode], frequency=tune.freq_hz / 1000.0,
        lp_cut=None, hp_cut=None, freq_pbc=False,
        compression=compression, resample=0,
        nb=False, nb_test=False, de_emp=False,
        ADC_OV=False, S_meter=-1, sdt=0, stats=False, tstamp=False, sound=True,
        netcat=False, wideband=False, camp_allow_1ch=False, rev_bin=False,
        dir=None, filename=None, station=None, test_mode=False, wf_cal=None,
        idx=0, tlimit=None, socket_timeout=10,
        ws_timestamp=int(time.time()) & 0xFFFFFFFF,
    )


class _KiwiAudioStream(K.KiwiSDRStream):
    """SND stream that pulls its tune state from the controller."""

    def __init__(self, options, owner, stop):
        super().__init__()
        self._options = options
        self._type = "SND"
        self._owner = owner
        self._stop_event = stop
        self._start_time = None
        self._camp_wait_event = None
        self.rx_ready = False

    # Replaces kiwiclient's version, whose redirect loop is unbounded, can't be
    # cancelled and leaks a socket per hop.
    def _prepare_stream(self, host, port, which):
        self._stream_name = which
        uri = "%s/%d/%s%s" % ("/wb" if self._options.wideband else "", self._options.ws_timestamp, which,
                              "?camp" if self._camp_chan != -1 else "")
        visited = []
        while True:
            if self._stop_event.is_set():
                raise K.KiwiConnectionError("connection cancelled")
            hop = "%s:%s" % (host, port)
            if hop in visited:
                raise KiwiRedirectLoopError("%s appears to be offline (redirect loop: %s)"
                                            % (visited[0], " -> ".join(visited + [hop])))
            if len(visited) > MAX_HTTP_REDIRECTS:
                raise KiwiRedirectLoopError("Too many redirects connecting to %s" % visited[0])
            visited.append(hop)
            log.info("URL: %s%s", hop, uri)
            self._socket = socket.create_connection((host, port), timeout=self._options.socket_timeout)
            location, status_code = K.ClientHandshakeProcessor(self._socket, host, port).handshake(uri)
            if status_code == "101":
                break
            self._socket.close()
            u = urlparse(location)
            host, port = u.hostname or host, u.port or port
            log.warning("HTTP %s redirect to %s:%s", status_code, host, port)

        request = K.ClientRequest(self._socket)
        request.ws_version = K.VERSION_HYBI13
        stream_option = K.StreamOptions()
        stream_option.mask_send = True
        stream_option.unmask_receive = False
        self._stream = K.Stream(request, stream_option)

    # kiwiclient logs every command at debug level, including the password in
    # 'SET auth'. Send that one ourselves with the password masked in the log.
    def _send_message(self, msg):
        if msg.startswith("SET auth"):
            log.debug("send SET (%s) \"%s\"", self._stream_name, re.sub(r" (p|ipl)=\S*", r" \1=***", msg))
            self._stream.send_message(msg)
            return
        super()._send_message(msg)

    # Called by kiwiclient when the server sends sample_rate (start of audio).
    def _setup_rx_params(self):
        self.set_name(self._options.user)
        self.apply_tune(self._owner.tune)
        self.set_agc(on=True)
        if not self._options.compression:
            self._set_snd_comp(False)
        self.rx_ready = True
        self._owner._on_rx_ready(self)

    def freq_limits_hz(self):
        lo_khz = self._kiwi_foff or 0.0
        return (max(MIN_FREQ_HZ, int(lo_khz * 1000)),
                int((lo_khz + self.MAX_FREQ) * 1000))

    def apply_tune(self, st):
        lo, hi = self.freq_limits_hz()
        if not lo <= st.freq_hz <= hi:
            clamped = min(max(st.freq_hz, lo), hi)
            self._owner._report_clamp(st.freq_hz, lo, hi, clamped)
            st = st.with_freq(clamped)
        lc, hc = passband_edges(st.mode, st.passband)
        self.set_mod(MODES[st.mode], lc, hc, st.freq_hz / 1000.0)

    def _process_aud(self, body):
        # Decode according to the per-packet flag, not our last request: packets
        # already in flight when 'SET compression=0' is sent are still ADPCM.
        if len(body) and not self._stereo:
            self._compression = bool(body[0] & 0x10)   # SND_FLAG_COMPRESSED
        super()._process_aud(body)

    def _process_audio_samples(self, seq, samples, rssi, fmt):
        self._owner._on_audio(samples, rssi)

    def _on_sample_rate_change(self):
        pass


class RadioController:
    def __init__(self, on_event=None, compression=True):
        self._on_event = on_event or (lambda kind, payload: None)
        self._lock = threading.RLock()
        self._tune = TuneState()
        self._dirty = False
        self._server = None
        self._compression = compression
        self._thread = None
        self._stop = threading.Event()
        self._stream = None
        self._connected = False
        self._rate = None
        self._limits = (MIN_FREQ_HZ, DEFAULT_MAX_FREQ_HZ)
        self._sink_lock = threading.Lock()
        self._sinks = {}       # 'card' / 'file' -> sink
        self._last_smeter = 0.0
        self._last_audio = None    # monotonic time of the last audio block
        self.rssi = None
        self.state = IDLE
        self.last_error = None
        self.connected_since = None    # monotonic, None while not connected
        self.reconnects = 0            # automatic reconnects since connect()
        self.gap_seconds = 0.0         # silence inserted into the current recording

    # ------------------------------------------------------------------ events
    def _emit(self, kind, payload=None):
        try:
            self._on_event(kind, payload)
        except Exception:
            log.exception("event handler failed for %s", kind)

    def _status(self, text, level="info"):
        getattr(log, "warning" if level == "error" else level)(text)
        self._emit("status", (text, level))

    def _set_state(self, state, error=None):
        self.state = state
        if error is not None or state in (CONNECTED, CONNECTING):
            self.last_error = error
        self._emit("state", (state, self.last_error))

    # ------------------------------------------------------------------ tune
    @property
    def tune(self):
        with self._lock:
            return self._tune

    @property
    def connected(self):
        return self._connected

    @property
    def server(self):
        return self._server

    def peer_address(self):
        """IP address of the Kiwi (or proxy) we are actually connected to, or None."""
        st = self._stream
        try:
            return st._socket.getpeername()[0] if st is not None and st._socket else None
        except OSError:
            return None

    @property
    def running(self):
        """True while a worker is trying to stay connected (any state but idle/failed)."""
        t = self._thread
        return t is not None and t.is_alive()

    @property
    def freq_limits(self):
        return self._limits

    def set_frequency(self, hz, source="ui"):
        hz = int(round(float(hz)))
        lo, hi = self._limits
        if not lo <= hz <= hi:
            raise ValueError("frequency %.3f kHz outside %.0f-%.0f kHz" % (hz / 1e3, lo / 1e3, hi / 1e3))
        with self._lock:
            if hz == self._tune.freq_hz:
                return self._tune
            self._tune = self._tune.with_freq(hz)
            self._dirty = True
            st = self._tune
        self._emit("tune", (st, source))
        return st

    def set_mode(self, mode, passband=None, source="ui"):
        if normalize_mode(mode) is None:
            raise ValueError("unsupported mode %r" % mode)
        with self._lock:
            new = self._tune.with_mode(mode, passband)
            if new == self._tune:
                return new
            self._tune = new
            self._dirty = True
        self._emit("tune", (new, source))
        return new

    # ------------------------------------------------------------------ connection
    def connect(self, server: ServerSpec):
        self.disconnect()
        self._server = server
        self.reconnects = 0
        self._set_state(CONNECTING)
        # Fresh event per worker: a previous worker that outlived disconnect()'s
        # join timeout must stay stopped instead of being revived by clear().
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(self._stop,), name="kiwi-worker", daemon=True)
        self._thread.start()

    def disconnect(self, timeout=3.0):
        t = self._thread
        if t is None:
            return
        self._stop.set()
        s = self._stream
        if s is not None and s._socket is not None:
            try:
                s._socket.shutdown(socket.SHUT_RDWR)   # unblock receive_message()
            except OSError:
                pass
        t.join(timeout)
        self._thread = None
        if self.state != FAILED:
            self._set_state(IDLE)

    def _set_connected(self, flag):
        if flag != self._connected:
            self._connected = flag
            self.connected_since = time.monotonic() if flag else None
            self._emit("connected", flag)

    def _run(self, stop):
        server = self._server
        redirects = 0
        failed = None
        first = True
        while not stop.is_set():
            delay = RECONNECT_DELAY
            stream = None
            if not first:
                self.reconnects += 1
                if self.state not in (BUSY,):
                    self._set_state(RECONNECTING, self.last_error)
            first = False
            try:
                self._status("Connecting to %s ..." % server.label())
                opts = _make_options(server, self.tune, self._compression)
                stream = _KiwiAudioStream(opts, self, stop)
                self._stream = stream
                stream.connect(server.host, server.port)
                stream.open()
                while not stop.is_set():
                    if stream.rx_ready and self._dirty:
                        with self._lock:
                            st, self._dirty = self._tune, False
                        try:
                            stream.apply_tune(st)
                        except Exception as e:
                            self._status("Tune failed: %s" % e, "error")
                    stream.run()
            except K.KiwiTooBusyError:
                msg = "%s: all channels busy, retrying in %ds" % (server.label(), BUSY_DELAY)
                self._status(msg, "error")
                self._set_state(BUSY, msg)
                delay = BUSY_DELAY
            except K.KiwiRedirectError as e:
                redirects += 1
                if redirects > MAX_HTTP_REDIRECTS:
                    failed = "%s: too many redirects, giving up" % self._server.label()
                    self._status(failed, "error")
                    break
                u = urlparse(str(e))
                if u.hostname:
                    server = ServerSpec(u.hostname, u.port or server.port, server.password, server.user)
                    self._server = server
                    self._status("Redirected to %s" % server.label())
                    delay = 1.0
            except (K.KiwiBadPasswordError, K.KiwiDownError) as e:
                failed = str(e)
                self._status(failed, "error")
                break
            except (K.KiwiServerTerminatedConnection, ConnectionTerminatedException,
                    K.KiwiConnectionError, OSError) as e:
                if stop.is_set():
                    break
                msg = "Connection lost (%s); reconnecting in %ds" % (e or type(e).__name__, RECONNECT_DELAY)
                self._status(msg, "error")
                self.last_error = msg
            except Exception as e:
                if stop.is_set():
                    break
                log.exception("worker error")
                msg = "Error: %s; reconnecting in %ds" % (e, RECONNECT_DELAY)
                self._status(msg, "error")
                self.last_error = msg
            finally:
                if self._stream is stream:
                    self._stream = None
                if stream is not None and stream.rx_ready:
                    redirects = 0
                self._set_connected(False)
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            stop.wait(delay)
        self._set_connected(False)
        if failed is not None:
            self._set_state(FAILED, failed)
        elif self._stop is stop:       # not superseded by a newer connect()
            self._set_state(IDLE)
        self._status("Disconnected" if failed is None else "Stopped: %s" % failed,
                     "info" if failed is None else "error")
        self._emit("stopped")

    # ------------------------------------------------------------------ stream callbacks
    def _on_rx_ready(self, stream):
        self._rate = stream._sample_rate
        self._limits = stream.freq_limits_hz()
        with self._sink_lock:
            self._fill_recording_gap()
            for name, sink in list(self._sinks.items()):
                self._sink_set_rate(name, sink)
        self._set_connected(True)
        self._set_state(CONNECTED)
        self._emit("rate", self._rate)
        self._emit("limits", self._limits)
        self._status("Connected to %s (%.1f Hz audio)" % (self._server.label(), self._rate))

    def _fill_recording_gap(self):
        """After a reconnect, pad the recording with silence for the time no audio
        arrived, so positions in the file keep matching wall-clock time."""
        sink = self._sinks.get("file")
        last, self._last_audio = self._last_audio, None
        if sink is None or last is None or not hasattr(sink, "write_silence"):
            return
        gap = time.monotonic() - last
        if gap < 0.5:
            return
        if getattr(sink, "rate", None) != int(round(self._rate)):
            return                       # new rate -> the sink starts a new file part
        gap = min(gap, MAX_GAP_FILL)
        sink.write_silence(gap)
        self.gap_seconds += gap
        log.info("recording: inserted %.1f s of silence for the reconnect gap", gap)

    def _report_clamp(self, want, lo, hi, got):
        with self._lock:
            self._tune = self._tune.with_freq(got)
            st = self._tune
        self._status("%.3f kHz is outside this receiver's range (%.0f-%.0f kHz); tuned %.3f kHz"
                     % (want / 1e3, lo / 1e3, hi / 1e3, got / 1e3), "error")
        self._emit("tune", (st, "clamp"))

    def _on_audio(self, samples, rssi):
        self.rssi = rssi
        now = time.monotonic()
        self._last_audio = now
        if now - self._last_smeter >= SMETER_INTERVAL:
            self._last_smeter = now
            self._emit("smeter", rssi)
        with self._sink_lock:
            for name, sink in list(self._sinks.items()):
                try:
                    sink.write(samples)
                except Exception as e:
                    log.exception("sink %s failed", name)
                    self._drop_sink(name, "Audio output '%s' stopped: %s" % (name, e))

    # ------------------------------------------------------------------ sinks
    def _sink_set_rate(self, name, sink):
        try:
            sink.set_input_rate(self._rate)
        except Exception as e:
            log.exception("sink %s open failed", name)
            self._drop_sink(name, "Audio output '%s' failed: %s" % (name, e))

    def _drop_sink(self, name, message):
        sink = self._sinks.pop(name, None)
        if sink is not None:
            try:
                sink.close()
            except Exception:
                pass
        self._status(message, "error")
        self._emit("sink_failed", name)

    def _install_sink(self, name, sink):
        with self._sink_lock:
            old = self._sinks.pop(name, None)
            if old is not None:
                old.close()
            if sink is not None:
                self._sinks[name] = sink
                if self._rate:
                    self._sink_set_rate(name, sink)
        return name in self._sinks if sink is not None else True

    def set_audio_device(self, device_key, gain=1.0):
        """device_key None -> stop sound-card output."""
        sink = SoundCardSink(device_key, gain) if device_key else None
        return self._install_sink("card", sink)

    def start_recording(self, path):
        sink = make_file_sink(path)
        if not self._connected:
            self._last_audio = None      # don't pad for time before this recording
        self.gap_seconds = 0.0
        ok = self._install_sink("file", sink)
        if ok:
            self._emit("recording", {"active": True, "path": path})
        return ok

    def stop_recording(self):
        with self._sink_lock:
            sink = self._sinks.pop("file", None)
        if sink is None:
            return None
        sink.close()
        info = {"active": False, "path": sink.current_path or sink.path, "seconds": sink.seconds(),
                "gap_seconds": round(self.gap_seconds, 1)}
        self._emit("recording", info)
        return info

    def recording_seconds(self):
        sink = self._sinks.get("file")
        return sink.seconds() if sink else None

    def recording_path(self):
        sink = self._sinks.get("file")
        return (sink.current_path or sink.path) if sink else None

    def audio_stats(self):
        sink = self._sinks.get("card")
        return sink.stats() if sink else {}

    def shutdown(self):
        self.disconnect()
        self.stop_recording()
        self._install_sink("card", None)
