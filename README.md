# KiwiSDR Remote

A cross-platform (Windows / macOS / Linux) desktop application for a remote KiwiSDR operation and control.  This is ideal to pass audio data into FLDigi or WSJTX for non amateur radio operators.  

- Choose the server, frequency, mode (USB / LSB / AM) and bandwidth.
- Send the audio to any output device. That includes a virtual audio cable, so WSJT-X, fldigi and similar programs can decode it.
- Record to **WAV** (16-bit PCM at the Kiwi's native ~12 kHz) or **M4A/MP4** (AAC).
- Built-in **hamlib rigctld server** (NET rigctl, model 2). External programs can read and set the frequency and mode, and the GUI follows those changes.

It uses [jks-prv/kiwiclient](https://github.com/jks-prv/kiwiclient) for the KiwiSDR websocket protocol.

Tested on:
- Mac OSX Golden Gate using BlackHole
- Windows 11 26H2 and VB Virtual Audio Cable

```
             ┌──────────────── KiwiSDR Remote ─────────────────┐
KiwiSDR ◄─ws─┤ worker thread: KiwiSDRStream (kiwiclient)        │
             │   SET mod/freq ◄── TuneState ◄── GUI (PySide6)   │
             │        │                  ▲                      │
             │        ▼ int16 @ ~12 kHz  └── rigctld TCP :4532 ◄┼── WSJT-X / fldigi / rigctl
             │   ├─ SoundCardSink → resample + drift lock ──────┼─► VB-Cable / BlackHole / Pulse sink
             │   └─ WavSink / M4aSink ──────────────────────────┼─► file
             └──────────────────────────────────────────────────┘
```

## Install and run

Requires Python 3.10 or newer.

```bash
git clone https://github.com/jeffreycentex/kiwisdr-remote.git && cd kiwisdr-remote
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python run_kiwiremote.py             # add --debug for protocol/rigctl logging
```
On Windows, `requirements.txt` skips `samplerate`, because pip may try to build it from source there. The app then uses linear-interpolation resampling.

Linux only: PortAudio is also required: `sudo apt install libportaudio2` (Debian/Ubuntu) or `sudo dnf install portaudio` (Fedora).

`samplerate` and `av` are optional. Without `samplerate`, the app falls back to linear-interpolation resampling. Without `av`, the M4A/MP4 formats are greyed out and WAV still works.

## Virtual audio cable setup

The app plays into the *input* side of the virtual cable. The decoding program records from the *output* side.

| OS | Install | In KiwiSDR Remote, play to | In WSJT-X / fldigi, input from |
|---|---|---|---|
| Windows | [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) | `CABLE Input (VB-Audio Virtual Cable)` | `CABLE Output (VB-Audio Virtual Cable)` |
| macOS | [BlackHole](https://existential.audio/blackhole/) (`brew install blackhole-2ch`) | `BlackHole 2ch` | `BlackHole 2ch` |
| Linux | none. Click **Create 'KiwiSDR' virtual cable**, or run the commands below | `PulseAudio/PipeWire sink: KiwiSDR` | `KiwiSDR-RX` |

Windows lists every device once per host API (MME, DirectSound, WASAPI). MME or WASAPI both work. The app always opens a device at its default sample rate and resamples to it.

On first start, the app pre-selects a device whose name looks like a virtual cable.

### Linux virtual cable

A Linux virtual cable has two parts:

- A **null sink** called `KiwiSDR`. KiwiSDR Remote plays into it.
- A **remapped source** called `KiwiSDR_RX`, shown as `KiwiSDR-RX`. It is built from the sink's monitor and is what WSJT-X, JS8Call or fldigi select as **Input**.

The remapped source is needed because WSJT-X and JS8Call list devices through Qt Multimedia, which often hides monitor sources such as "Monitor of KiwiSDR". Without it, the KiwiSDR sink appears under Output but nothing usable appears under Input. For listen-only use, Output can be any device, because nothing is transmitted.

These commands create the cable until the next logout or reboot. The GUI button runs the same commands and skips any part that already exists:

```bash
pactl load-module module-null-sink sink_name=KiwiSDR sink_properties=device.description=KiwiSDR
pactl load-module module-remap-source master=KiwiSDR.monitor source_name=KiwiSDR_RX source_properties=device.description=KiwiSDR-RX
```

Restart WSJT-X or JS8Call afterwards, because they only list audio devices at startup.

To make it permanent on **PipeWire** (Fedora, Ubuntu 22.10+), create the file `~/.config/pipewire/pipewire-pulse.conf.d/kiwisdr.conf`, then run `systemctl --user restart pipewire-pulse`:

```
pulse.cmd = [
  { cmd = "load-module" args = "module-null-sink sink_name=KiwiSDR sink_properties=device.description=KiwiSDR" flags = [ ] }
  { cmd = "load-module" args = "module-remap-source master=KiwiSDR.monitor source_name=KiwiSDR_RX source_properties=device.description=KiwiSDR-RX" flags = [ ] }
]
```

To make it permanent on **PulseAudio**, add these lines to `~/.config/pulse/default.pa`:

```
.include /etc/pulse/default.pa
load-module module-null-sink sink_name=KiwiSDR sink_properties=device.description=KiwiSDR
load-module module-remap-source master=KiwiSDR.monitor source_name=KiwiSDR_RX source_properties=device.description=KiwiSDR-RX
```

If WSJT-X still shows **no input devices at all**:

- `pactl list short sources` should list `KiwiSDR_RX`. If it doesn't, the module didn't load.
- A **Flatpak** build needs sound-server access: `flatpak override --user --socket=pulseaudio <app-id>`. A **snap** build needs its `audio-record` interface connected.
- If Qt uses ALSA rather than Pulse, install `pipewire-alsa`, or `alsa-plugins-pulseaudio` / `libasound2-plugins` on PulseAudio. The `pulse`/`default` input then appears, and you can route it to `KiwiSDR-RX` in `pavucontrol` → Recording.

PortAudio cannot address individual Pulse sinks. The app works around this by opening PortAudio's `pulse` device and setting `PULSE_SINK` to the selected sink. Plain ALSA devices, including `snd-aloop` loopback, appear in the list as normal devices.

## rigctld (CAT control)

The rigctld server is on by default and listens on `127.0.0.1:4532`, the standard hamlib port. Set the address to `0.0.0.0` to allow clients from other machines on your LAN.

**WSJT-X / JTDX / JS8Call:** in Settings → Radio:

- Rig: `Hamlib NET rigctl`
- Network Server: `127.0.0.1:4532`
- PTT Method: `VOX`
- Mode: `None` or `USB`
- Split: `None`

Click *Test CAT*.

**fldigi:** in Configure → Rig Control → Hamlib, select rig `Hamlib NET rigctl` and set the device to `127.0.0.1:4532`.

**Command line:**

```bash
rigctl -m 2 -r 127.0.0.1:4532 F 14074000   # set frequency
rigctl -m 2 -r 127.0.0.1:4532 M USB 2800   # set mode + passband
rigctl -m 2 -r 127.0.0.1:4532 f m l STRENGTH
```

Supported commands:

- `f/F` get/set frequency
- `m/M` get/set mode and passband
- `v/V`, `s/S`, `t/T`: compatibility stubs (receive only, PTT accepted and ignored)
- `l STRENGTH`: S-meter in dB relative to S9
- `\dump_state`, `\chk_vfo`, `\get_powerstat`
- `+` extended responses
- A leading VFO argument (`f VFOA`, `l VFOA STRENGTH`, `\set_freq currVFO 7074000`), as sent by fldigi and other VFO-mode clients, is accepted and ignored.

Mode mapping: `PKTUSB`/`USBD` map to USB, `PKTLSB` to LSB, `SAM`/`AMS` to AM. Other modes return `RPRT -11`. Changes made through rigctl update the GUI immediately and are applied to the Kiwi whether or not it is connected at that moment.

## Behaviour notes

- **Passband.** SSB uses a 200 Hz low edge plus the bandwidth (default 2800 Hz, giving 200–3000 Hz). AM is symmetric (default ±4900 Hz). A rigctl passband of 0 means the mode's default.
- **Clock drift.** The Kiwi's sample clock and your sound card's clock differ by tens to hundreds of ppm. A PI controller holds the output buffer at 200 ms (measured when an on-time packet arrives) by trimming the resampling ratio, by at most ±0.5 %. The trim settles to the true clock offset within a few minutes. Network jitter moves the pitch by much less than 0.1 %, about 1 Hz at a 1500 Hz FT8 tone. The current trim and buffer level are shown under the device selector.
- **Output blocks.** The device is opened with ~20 ms callback blocks and ~100 ms of device latency. Smaller blocks (e.g. BlackHole's 1.3 ms minimum) cannot be served reliably from Python. Gaps after an underrun are faded in and out, and gain above 100 % goes through a soft limiter instead of hard clipping.
- **Network jitter.** Kiwi packets carry up to 170 ms of audio and arrive with jitter. The buffer level is corrected by each packet's lateness, so jitter doesn't disturb the trim. After a network stall, the late backlog is dropped when playback resumes, so the stall can't wind up the trim.
- **Reconnects.** If the connection drops, the app reconnects after 5 s. If all channels are busy, it retries every 15 s. A bad password stops retrying.
- **Frequency range** is taken from the server after connecting. This also covers Kiwis with a configured frequency offset (transverter or LF setups). An out-of-range request is clamped and reported.
- **Compressed audio (ADPCM)** is on by default, as in the Kiwi web UI. Turning it off uses about 4× the bandwidth (192 kbit/s) but is lossless. The setting applies on the next connect.
- **WAV** files are written at the Kiwi's integer sample rate (12001 Hz, say). If you change server mid-recording and the rate changes, a new `_1.wav` part is started. Classic WAV is limited to 4 GB, which is about 49 hours of mono audio at 12 kHz.
- **M4A/MP4** files are AAC mono, 64 kbit/s, 48 kHz, encoded with PyAV's bundled FFmpeg.
- Settings are saved between runs with QSettings: server history, frequency, mode, device, folder and rigctld settings. Passwords are not saved.

## Tests

```bash
python -m unittest discover -s tests -v
```

The tests are headless and need no audio hardware or real Kiwi. They cover:

- A fake KiwiSDR websocket server (`tests/fake_kiwi.py`) sending ADPCM-compressed and uncompressed audio. The tests connect, tune via the GUI API and via rigctl, check the exact `SET mod=` commands, test auto-reconnect and the busy-server path, and check the recorded WAV content.
- The rigctld protocol: short, long and extended forms, `dump_state`, multiple commands per line, and VFO arguments.
- The resampler's continuity and length accuracy.
- A simulated-time run of the drift controller with sound-card clocks at ±300 ppm.

## Building standalone apps

Build on each target OS; PyInstaller does not cross-compile.

```bash
pip install pyinstaller
python fetch_kiwiclient.py
pyinstaller kiwiremote.spec
```

The output is `dist/KiwiSDR Remote/KiwiSDR Remote.exe` on Windows, `dist/KiwiSDR Remote.app` on macOS (unsigned, so right-click → Open the first time), and `dist/KiwiSDR Remote/KiwiSDR Remote` on Linux.

## Licensing

KiwiSDR Remote is free software, licensed under the **GNU General Public License v3.0 or later**. See [LICENSE](LICENSE).

`third_party/kiwiclient` is a pinned copy of [jks-prv/kiwiclient](https://github.com/jks-prv/kiwiclient). It is included so the repo works right after cloning, and its files keep their own licenses: parts are GPL-2.0-or-later, and `mod_pywebsocket` is BSD. This app does not use kiwiclient's rigctld; it has its own implementation in `kiwiremote/rigctld.py`.

## Layout

```
run_kiwiremote.py        launcher / PyInstaller entry
fetch_kiwiclient.py      downloads kiwiclient into third_party/
kiwiremote/
  gui.py                 PySide6 main window
  radio.py               connection worker, controller API, reconnect logic
  rigctld.py             hamlib NET rigctl server
  sinks.py               sound-card (drift-locked), WAV and M4A/MP4 outputs
  resample.py            streaming resampler (libsamplerate or linear)
  state.py               tune/server models, mode and passband rules
  kiwi_import.py         locates kiwiclient
tests/                   fake Kiwi server + unit/integration tests
```
