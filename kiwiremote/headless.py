# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless mode: service + HTTP API + rigctld, no Qt (works without a desktop).

    python -m kiwiremote --headless [options]

Stops cleanly on Ctrl+C / SIGTERM, closing any WAV recording properly.
"""

import argparse
import logging
import os
import signal
import threading

from .api import DEFAULT_PORT, ApiServer
from .rigctld import RigctldServer
from .service import KiwiService, SessionPolicy, default_record_dir

log = logging.getLogger(__name__)


def build_parser():
    ap = argparse.ArgumentParser(prog="kiwiremote --headless",
                                 description="Run KiwiSDR Remote without a window")
    ap.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--debug", action="store_true")
    g = ap.add_argument_group("API")
    g.add_argument("--api-host", default="127.0.0.1")
    g.add_argument("--api-port", type=int, default=DEFAULT_PORT)
    g.add_argument("--api-token", default=os.environ.get("KIWIREMOTE_API_TOKEN"),
                   help="require this token (default: $KIWIREMOTE_API_TOKEN)")
    g = ap.add_argument_group("rigctld")
    g.add_argument("--rigctl-host", default="127.0.0.1")
    g.add_argument("--rigctl-port", type=int, default=4532)
    g.add_argument("--no-rigctl", action="store_true")
    g = ap.add_argument_group("audio and recording")
    g.add_argument("--audio-device", help="play to the output device whose name contains this text")
    g.add_argument("--record-dir", default=default_record_dir())
    g.add_argument("--no-compression", action="store_true", help="request uncompressed audio")
    g = ap.add_argument_group("startup connection (optional)")
    g.add_argument("--connect", metavar="HOST[:PORT]")
    g.add_argument("--password", default=os.environ.get("KIWIREMOTE_KIWI_PASSWORD", ""))
    g.add_argument("--user", default="kiwiremote")
    g.add_argument("--freq-khz", type=float)
    g.add_argument("--mode", choices=["USB", "LSB", "AM"])
    g = ap.add_argument_group("session policy (minutes, 0 = off)")
    d = SessionPolicy()
    g.add_argument("--limit-password-min", type=float, default=d.limit_with_password_min)
    g.add_argument("--limit-public-min", type=float, default=d.limit_without_password_min)
    g.add_argument("--reset-interval-min", type=float, default=d.reset_interval_min)
    g.add_argument("--reset-pause-s", type=float, default=d.reset_pause_s)
    g.add_argument("--no-local-exempt", action="store_true",
                   help="apply limits and resets to Kiwis on loopback/LAN addresses too")
    return ap


def _pick_device(text):
    from .sinks import list_output_devices
    devs = list_output_devices()
    for key, label in devs:
        if text.lower() in label.lower():
            return key
    raise SystemExit("no output device matches %r; available:\n  %s"
                     % (text, "\n  ".join(lbl for _, lbl in devs) or "(none)"))


def run(argv):
    a = build_parser().parse_args(argv)
    logging.getLogger().setLevel(logging.DEBUG if a.debug else logging.INFO)
    policy = SessionPolicy(a.limit_password_min, a.limit_public_min, a.reset_interval_min,
                           a.reset_pause_s, not a.no_local_exempt)
    svc = KiwiService(policy=policy, compression=not a.no_compression, record_dir=a.record_dir)
    api = ApiServer(svc, a.api_host, a.api_port, a.api_token)
    rig = None if a.no_rigctl else RigctldServer(svc.radio, a.rigctl_host, a.rigctl_port)
    done = threading.Event()

    def _stop(signum, frame):
        log.info("signal %d: shutting down", signum)
        done.set()

    # SIGBREAK = Ctrl+Break on Windows, also how scripts/service wrappers stop a console program there
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):      # not main thread / unsupported (Windows SIGTERM)
            pass
    try:
        api.start()
        if rig is not None:
            rig.start()
        if a.audio_device:
            svc.radio.set_audio_device(_pick_device(a.audio_device))
        if a.mode or a.freq_khz:
            svc.tune(a.freq_khz * 1000 if a.freq_khz else None, a.mode, source="startup")
        if a.connect:
            svc.connect(a.connect, password=a.password, user=a.user)
        log.info("headless: API %s%s; Ctrl+C to quit", api.url,
                 "" if rig is None else ", rigctld %s:%d" % (rig.host, rig.port))
        while not done.wait(0.5):
            pass
    finally:
        api.stop()
        if rig is not None:
            rig.stop()
        svc.shutdown()
    return 0
