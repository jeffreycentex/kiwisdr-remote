# SPDX-License-Identifier: GPL-3.0-or-later
"""Qt-free application service: the one place that connects, tunes, records and
enforces the session policy. The GUI, the HTTP API and headless mode all drive
this class, so whatever one of them does, the others see.

Session policy (all values configurable; 0 turns an item off)
--------------------------------------------------------------
* Session limit: total connected time per connect() call. Default: none when
  a password was given (the session keeps going, with periodic resets), 45 min
  without. At the limit the recording is closed and the connection dropped.
* Periodic reset: every 120 min of connection, disconnect for 15 s and
  reconnect, which is polite to Kiwi operators. A session limit, if any,
  keeps counting across resets.
* Servers reached over loopback, private (RFC 1918 / ULA) or link-local
  addresses are exempt from both by default: that is your own Kiwi.
  Locality is decided from the address actually connected to, after connecting.
* Explicit per-connect overrides (session_limit_min / reset_interval_min)
  always win, including for local servers.

Locking: the radio emits events from its worker thread, and disconnect()
joins that thread. Never call radio.connect()/disconnect() while holding
self._lock, and never take self._lock in the event handler.
"""

import dataclasses
import ipaddress
import logging
import os
import re
import threading
import time

from . import __version__
from .radio import BUSY, CONNECTED, FAILED, RadioController
from .state import ServerSpec

log = logging.getLogger(__name__)

TICK_SECONDS = 0.5


@dataclasses.dataclass
class SessionPolicy:
    limit_with_password_min: float = 0.0     # 0 = no limit: keep going, reset every 2 h
    limit_without_password_min: float = 45.0
    reset_interval_min: float = 120.0
    reset_pause_s: float = 15.0
    exempt_local: bool = True

    @classmethod
    def from_dict(cls, d):
        p = cls()
        for f in dataclasses.fields(cls):
            if f.name in (d or {}):
                setattr(p, f.name, f.type(d[f.name]) if f.type is not bool else bool(d[f.name]))
        return p


def is_local_address(addr):
    """True for loopback, private, link-local and unique-local addresses."""
    if not addr:
        return False
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


class ApiError(Exception):
    """Error with a stable machine-readable code (used by the HTTP API)."""

    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


_SAFE = re.compile(r"[^A-Za-z0-9._+-]+")


def _safe(text):
    return _SAFE.sub("_", str(text)).strip("._") or "x"


class _Session:
    def __init__(self, spec, limit_override, reset_override):
        self.spec = spec
        self.limit_override = limit_override      # minutes, None = use policy
        self.reset_override = reset_override
        self.started = None        # service clock at first CONNECTED
        self.connected_at = None   # service clock at the latest CONNECTED
        self.local = None          # decided after connecting
        self.limit_s = None
        self.reset_s = None
        self.resets = 0
        self.reset_until = None    # service clock when a reset pause ends
        self.ended = None          # reason, once over


class KiwiService:
    def __init__(self, on_event=None, policy=None, compression=True, record_dir=None,
                 clock=time.monotonic, start_ticker=True):
        self._on_event = on_event or (lambda kind, payload: None)
        self.policy = policy or SessionPolicy()
        self.record_dir = record_dir or default_record_dir()
        self._clock = clock
        self._lock = threading.RLock()
        self._session = None
        self._rec_deadline = None
        self._rec_max = None
        self.radio = RadioController(on_event=self._on_radio_event, compression=compression)
        self._stop = threading.Event()
        self._ticker = None
        if start_ticker:
            self._ticker = threading.Thread(target=self._tick_loop, name="kiwi-service", daemon=True)
            self._ticker.start()

    # ------------------------------------------------------------------ events
    def _emit(self, kind, payload=None):
        try:
            self._on_event(kind, payload)
        except Exception:
            log.exception("event handler failed for %s", kind)

    def _on_radio_event(self, kind, payload):
        # Runs on the worker thread: no self._lock here (see module docstring).
        if kind == "state" and payload[0] == CONNECTED:
            s = self._session
            if s is not None and s.ended is None:
                now = self._clock()
                if s.started is None:
                    s.started = now
                s.connected_at = now
                if s.local is None:
                    s.local = is_local_address(self.radio.peer_address())
                    self._apply_policy(s)
                self._emit("session", self.session_info())
        self._emit(kind, payload)

    def _apply_policy(self, s):
        p = self.policy
        exempt = p.exempt_local and s.local
        if s.limit_override is not None:
            limit = s.limit_override
        elif exempt:
            limit = 0
        else:
            limit = p.limit_with_password_min if s.spec.password else p.limit_without_password_min
        if s.reset_override is not None:
            reset = s.reset_override
        else:
            reset = 0 if exempt else p.reset_interval_min
        s.limit_s = limit * 60.0 if limit and limit > 0 else None
        s.reset_s = reset * 60.0 if reset and reset > 0 else None

    # ------------------------------------------------------------------ connection
    def connect(self, server, password="", user=None, wait=0.0,
                session_limit_min=None, reset_interval_min=None, compression=None):
        """server: 'host[:port]' text or ServerSpec. Returns status()."""
        if isinstance(server, ServerSpec):
            spec = server
        else:
            try:
                spec = ServerSpec.parse(server, password or "", user or "kiwiremote")
            except (ValueError, TypeError) as e:
                raise ApiError("bad_request", "invalid server address: %s" % e)
        for name, v in (("session_limit_min", session_limit_min), ("reset_interval_min", reset_interval_min)):
            if v is not None and (not isinstance(v, (int, float)) or v < 0):
                raise ApiError("bad_request", "%s must be a number >= 0" % name)
        with self._lock:
            self._session = _Session(spec, session_limit_min, reset_interval_min)
        if compression is not None:
            self.radio._compression = bool(compression)
        self.radio.connect(spec)
        self._emit("session", self.session_info())
        if wait and wait > 0:
            end = time.monotonic() + float(wait)
            while time.monotonic() < end and self.radio.state not in (CONNECTED, FAILED, BUSY):
                time.sleep(0.05)
        return self.status()

    def disconnect(self, reason="requested"):
        """Stops the recording too: a disconnected recording would only collect gaps."""
        rec = self.stop_recording(reason="disconnect")
        with self._lock:
            s = self._session
            if s is not None and s.ended is None:
                s.ended = reason
        self.radio.disconnect()
        self._emit("session", self.session_info())
        st = self.status()
        st["recording_stopped"] = rec
        return st

    # ------------------------------------------------------------------ tuning
    def tune(self, freq_hz=None, mode=None, passband=None, source="api"):
        try:
            if mode is not None or passband is not None:
                self.radio.set_mode(mode or self.radio.tune.mode, passband, source=source)
            if freq_hz is not None:
                self.radio.set_frequency(freq_hz, source=source)
        except (ValueError, TypeError) as e:
            raise ApiError("bad_request", str(e))
        return self.tune_info()

    def tune_info(self):
        st = self.radio.tune
        return {"freq_hz": st.freq_hz, "mode": st.mode, "passband": st.passband}

    # ------------------------------------------------------------------ recording
    def recording_path_for(self, path=None, directory=None, name=None, formats=(".wav",)):
        if path:
            path = os.path.expanduser(path)
            root, ext = os.path.splitext(path)
            if ext.lower() not in formats:
                if ext:
                    raise ApiError("bad_request", "only %s recordings are supported" % "/".join(formats))
                path = root + formats[0]
        else:
            directory = os.path.expanduser(directory or self.record_dir)
            st = self.radio.tune
            now = time.gmtime()
            spec = self._session.spec if self._session else None
            fields = {
                "date": time.strftime("%Y%m%d", now),
                "time": time.strftime("%H%M%SZ", now),
                "freq_khz": "%.3f" % (st.freq_hz / 1000.0),
                "mode": st.mode,
                "server": spec.host if spec else "none",
            }
            template = name or "kiwi_{date}T{time}_{freq_khz}kHz_{mode}"
            try:
                base = template.format(**fields)
            except (KeyError, IndexError, ValueError) as e:
                raise ApiError("bad_request", "bad name template (fields: %s): %s"
                               % (", ".join(sorted(fields)), e))
            base = _safe(os.path.splitext(base)[0] if base.lower().endswith(".wav") else base)
            path = os.path.join(directory, base + ".wav")
        root, ext = os.path.splitext(path)
        n = 1
        while os.path.exists(path):            # never overwrite an earlier recording
            path = "%s_%d%s" % (root, n, ext)
            n += 1
        return path

    def start_recording(self, path=None, directory=None, name=None, max_seconds=None,
                        formats=(".wav",)):
        """The API allows WAV only; the GUI passes formats to also allow .m4a/.mp4."""
        if max_seconds is not None and (not isinstance(max_seconds, (int, float)) or max_seconds <= 0):
            raise ApiError("bad_request", "max_seconds must be a number > 0")
        if self.radio.recording_path() is not None:
            raise ApiError("conflict", "already recording to %s" % self.radio.recording_path(), 409)
        path = self.recording_path_for(path, directory, name, formats)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self.radio.start_recording(path)
        except (OSError, RuntimeError, ValueError) as e:
            raise ApiError("io_error", "cannot record to %s: %s" % (path, e), 500)
        with self._lock:
            self._rec_max = float(max_seconds) if max_seconds else None
            self._rec_deadline = self._clock() + self._rec_max if self._rec_max else None
        return self.recording_info()

    def stop_recording(self, reason="requested"):
        with self._lock:
            self._rec_deadline = self._rec_max = None
        info = self.radio.stop_recording()
        if info is not None:
            info["reason"] = reason
            log.info("recording stopped (%s): %s, %.1f s", reason, info["path"], info["seconds"])
        return info

    def recording_info(self):
        path = self.radio.recording_path()
        if path is None:
            return {"active": False}
        d = {"active": True, "path": path,
             "seconds": round(self.radio.recording_seconds() or 0.0, 1),
             "gap_seconds": round(self.radio.gap_seconds, 1),
             "waiting_for_audio": not self.radio.connected}
        if self._rec_deadline is not None:
            d["max_seconds"] = self._rec_max
            d["remaining_s"] = max(0, round(self._rec_deadline - self._clock()))
        return d

    # ------------------------------------------------------------------ status
    def session_info(self):
        s = self._session
        if s is None:
            return None
        now = self._clock()
        d = {"server": s.spec.label(), "password": bool(s.spec.password),
             "local": s.local, "limit_s": s.limit_s, "reset_interval_s": s.reset_s,
             "resets": s.resets, "reconnects": self.radio.reconnects, "ended": s.ended}
        if s.started is not None:
            d["connected_for_s"] = round(now - s.started)
            if s.limit_s:
                d["remaining_s"] = max(0, round(s.started + s.limit_s - now))
        if s.reset_until is not None:
            d["reset_in_progress_s"] = max(0, round(s.reset_until - now))
        elif s.reset_s and s.connected_at is not None and self.radio.connected:
            d["next_reset_s"] = max(0, round(s.connected_at + s.reset_s - now))
        return d

    def status(self):
        r = self.radio
        return {
            "app_version": __version__,
            "state": r.state,
            "connected": r.connected,
            "server": r.server.label() if r.server else None,
            "last_error": r.last_error,
            "tune": self.tune_info(),
            "s_meter_dbm": None if r.rssi is None or not r.connected else round(r.rssi, 1),
            "session": self.session_info(),
            "recording": self.recording_info(),
            "audio_output": "card" in r._sinks,
        }

    # ------------------------------------------------------------------ timers
    def _tick_loop(self):
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception:
                log.exception("service tick failed")

    def tick(self):
        """Enforce recording max length, session limit and periodic reset."""
        now = self._clock()
        action = None
        with self._lock:
            if self._rec_deadline is not None and now >= self._rec_deadline:
                action = ("rec_max",)
            s = self._session
            if action is None and s is not None and s.ended is None:
                if s.limit_s and s.started is not None and now - s.started >= s.limit_s:
                    s.ended = "session limit"
                    action = ("limit", s.limit_s)
                elif s.reset_until is not None:
                    if now >= s.reset_until:
                        s.reset_until = None
                        s.resets += 1
                        action = ("reconnect", s.spec)
                elif (s.reset_s and s.connected_at is not None and self.radio.connected
                        and now - s.connected_at >= s.reset_s):
                    s.reset_until = now + self.policy.reset_pause_s
                    s.connected_at = None
                    action = ("reset",)
        if action is None:
            return
        kind = action[0]
        if kind == "rec_max":
            info = self.stop_recording(reason="max_seconds")
            self._status("Recording reached its maximum length%s"
                         % (": %s" % os.path.basename(info["path"]) if info else ""))
        elif kind == "limit":
            info = self.stop_recording(reason="session limit")
            self.radio.disconnect()
            self._status("Session limit of %d min reached; disconnected%s"
                         % (round(action[1] / 60), " and recording closed" if info else ""), "error")
            self._emit("session", self.session_info())
        elif kind == "reset":
            self._status("Periodic session reset: reconnecting in %d s" % self.policy.reset_pause_s)
            self.radio.disconnect()
            self._emit("session", self.session_info())
        elif kind == "reconnect":
            self.radio.connect(action[1])
            self._emit("session", self.session_info())

    def _status(self, text, level="info"):
        (log.warning if level == "error" else log.info)(text)
        self._emit("status", (text, level))

    # ------------------------------------------------------------------ lifecycle
    def shutdown(self):
        self._stop.set()
        self.stop_recording(reason="shutdown")
        self.radio.shutdown()
        if self._ticker is not None:
            self._ticker.join(2.0)


def default_record_dir():
    return os.path.join(os.path.expanduser("~"), "Music", "KiwiSDR")
