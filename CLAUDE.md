# CLAUDE.md

Guidance for Claude, and for other contributors, working in this repository.

## What this is
KiwiSDR Remote is a desktop app (Windows, macOS, Linux; PySide6 GUI) for listening to a remote KiwiSDR. It tunes the Kiwi, plays its audio to an output device (usually a virtual cable feeding fldigi, WSJT-X or JS8Call), records WAV or M4A, and offers two control interfaces:
- a hamlib **rigctld** server (port 4532), for frequency and mode
- an **HTTP/JSON API** (`/api/v1`, port 7373), for server selection, recording and session handling

A **headless mode** runs everything except the GUI. A companion project, [fldigi-net-scheduler](https://github.com/jeffreycentex/fldigi-net-scheduler), drives this app through the API and through fldigi → rigctld.

The app is **receive-only**. rigctld accepts PTT commands and ignores them.

## Layout
- `kiwiremote/state.py`: tune and server models, mode aliases, passband rules. Frequencies are in **Hz**; the Kiwi wire protocol uses kHz, and conversion happens only in `radio.py`.
- `kiwiremote/radio.py`: `RadioController`. One worker thread owns the Kiwi websocket. It handles reconnects, connection states (`idle/connecting/connected/reconnecting/busy/failed`), audio sinks and gap filling. `_KiwiAudioStream` overrides parts of kiwiclient (redirect handling, password masking, the per-packet compression flag).
- `kiwiremote/service.py`: `KiwiService`. **Qt-free.** It is the single entry point for connect, disconnect, tune and record, and it enforces the session policy (limits, periodic reset, local exemption) and the recording `max_seconds`. The GUI, API and headless mode all call it. Put testable logic here.
- `kiwiremote/api.py`: stdlib `ThreadingHTTPServer`. Routes are a dict of `(method, path) -> handler`.
- `kiwiremote/ctl.py`: the `ctl` CLI, plus `ApiClient` (stdlib only, so other projects can copy it).
- `kiwiremote/headless.py`: the `--headless` entry point. It must never import PySide6.
- `kiwiremote/gui.py`: GUI only. Events arrive from other threads through `_Bridge` (a queued Qt signal). Buttons only *request* actions; widget state is updated from events (`state`, `recording`, `session`, `tune`), so changes made by the API and rigctld show up in the GUI.
- `kiwiremote/sinks.py`: `SoundCardSink` (drift-locked resampling, see the README), `WavSink`, `M4aSink`, and Linux PulseAudio/PipeWire helpers.
- `kiwiremote/rigctld.py`: the NET rigctl (model 2) subset that fldigi and WSJT-X use.
- `third_party/kiwiclient`: a pinned copy of jks-prv/kiwiclient (GPL). Don't edit it. Override its behaviour in `radio.py` instead, and update it with `fetch_kiwiclient.py`.
- `tests/fake_kiwi.py`: a fake KiwiSDR websocket server that sends ADPCM or raw audio and supports busy and password modes. Tests run against it.

## Run / test
```bash
python -m pip install -r requirements.txt
python run_kiwiremote.py [--debug]                 # GUI
python run_kiwiremote.py --headless --help          # no window
python run_kiwiremote.py ctl status                 # API client
python -m unittest discover -s tests -v             # needs only numpy
```
- The tests are headless: they need no Qt, PortAudio or audio hardware. They must pass on Linux, Windows and macOS, because CI runs them on all three (`.github/workflows/ci.yml`).
- In a sandbox without PySide6, GUI logic can be exercised with a stub PySide6 package on `sys.path` together with `fake_kiwi`. That catches logic errors, not Qt API misuse. Say so when the GUI hasn't run under real Qt.
- Builds: `.github/workflows/build.yml` runs PyInstaller with `kiwiremote.spec`. That produces a windowed `KiwiSDR Remote` and a console `kiwiremote-cli` sharing one bundle. Pushing a `v*` tag creates a release.

## Threading and locking rules
- Only the worker thread touches the Kiwi socket. Tune changes set a dirty flag that the worker applies between messages.
- `RadioController.disconnect()` joins the worker thread. Never call `radio.connect()` or `radio.disconnect()` while holding `KiwiService._lock`, and never take that lock in `_on_radio_event`, which runs on the worker thread. Doing either deadlocks.
- `KiwiService` takes an injectable `clock` and `start_ticker=False`, so tests can call `tick()` on simulated time. Use that for anything time-based; don't sleep for minutes in tests.

## Session policy (specified by the user; don't change it silently)
- The limit is 120 min with a password and 45 min without. The limit is total connected time; resets don't restart it. When the limit is reached, the recording is closed and the app disconnects.
- The periodic reset is every 120 min of connection: disconnect, wait 15 s, reconnect. The recording continues, and silence is inserted for the gap.
- Kiwis on loopback, private or link-local **peer** addresses are exempt from both. The user's own Kiwi is on the LAN.
- Per-connect overrides (`session_limit_min`, `reset_interval_min`) win, even for local Kiwis. `0` means off.

## API rules
- The API stays versioned under `/api/v1`. Additive changes only: new optional fields and new endpoints. A breaking change goes in `/api/v2`. Keep `docs/API.md` in step with the code, and keep error codes stable.
- Local-only by default. Keep the three browser protections: JSON content type required, loopback `Host` header, no CORS. Binding to a non-loopback address requires a token.
- The API records **WAV only**; only the GUI may pass `formats` that allow M4A.
- Never log or return Kiwi passwords. `SET auth` is masked in debug logs.

## Conventions
- License: GPL-3.0-or-later. New source files get the `# SPDX-License-Identifier: GPL-3.0-or-later` header.
- Use the standard library for the API and CLI. Runtime dependencies are PySide6, numpy and sounddevice; samplerate and av are optional. `samplerate` is skipped on Windows through a requirements marker.
- Recordings never overwrite existing files; `_1`, `_2`, … is appended.
- Python 3.10 or newer (CI tests 3.10 and 3.13).
- Commit with a descriptive body, and reference issues with "Fixes #N".
