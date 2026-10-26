# KiwiSDR Remote control API (v1)

The API is a small HTTP/JSON interface for schedulers and scripts, such as [FLDigi Net Scheduler](https://github.com/jeffreycentex/fldigi-net-scheduler). With it, a program can:

- connect to a KiwiSDR, with or without a password, and disconnect
- set the frequency, mode and passband
- start and stop WAV recordings

The GUI shows every change the API makes, and API calls see every change made in the GUI. Both drive the same service.

rigctld (port 4532) still handles frequency and mode for WSJT-X and fldigi. The API adds what rigctl can't do: choosing a server and password, recording, and session handling.

## Enabling it

There are two ways to run the API:

- **GUI:** in the *Remote control API and session limits* box, tick **API on 127.0.0.1, port** (default port 7373). You can optionally set a token.
- **Headless:** run `python run_kiwiremote.py --headless`. The API is always on in this mode. See [Headless mode](#headless-mode).

The base URL is `http://127.0.0.1:7373/api/v1`.

## Conventions

- Request and response bodies are JSON. **POST requests must send `Content-Type: application/json`**, even with an empty body (`{}`).
- Every response has `"ok": true` or `"ok": false`. Errors also carry `{"error": {"code": "...", "message": "..."}}`. The `code` values are stable and safe to branch on; the `message` is for people to read.
- Frequencies are in Hz (`freq_hz`). `/tune` also accepts `freq_khz`.
- Times are in seconds. Session-policy values are in minutes, where `0` means off.
- An incompatible future version would be added as `/api/v2` next to `/api/v1`. New fields may appear in v1 responses, so clients should ignore keys they don't know.

| HTTP | `error.code` | When |
|---|---|---|
| 400 | `bad_request`, `bad_json` | missing or invalid parameter, or malformed JSON |
| 401 | `unauthorized` | a token is configured, and the request's token is missing or wrong |
| 403 | `forbidden_host` | the `Host` header isn't `localhost`, `127.0.0.1` or `[::1]` |
| 404 / 405 | `not_found`, `method_not_allowed` | unknown path, or wrong HTTP method |
| 409 | `conflict` | a recording is already running |
| 413 / 415 | `too_large`, `unsupported_media_type` | the body is over 64 KB, or isn't JSON |
| 500 | `io_error`, `internal_error` | the file can't be written, or an unexpected error |
| 502 / 503 / 504 | `connect_failed`, `busy`, `timeout` | outcomes of `/connect` when called with `wait` |

## Endpoints

### `GET /api/v1`
Returns API information: `api_version`, `app_version`, and the list of endpoints.

### `GET /api/v1/status`
```json
{
  "ok": true,
  "app_version": "0.2.0",
  "state": "connected",
  "connected": true,
  "server": "kiwi.example.net:8073",
  "last_error": null,
  "tune": {"freq_hz": 7110000, "mode": "LSB", "passband": 2800},
  "s_meter_dbm": -84.2,
  "session": {
    "server": "kiwi.example.net:8073", "password": true, "local": false,
    "limit_s": 7200, "remaining_s": 5321, "connected_for_s": 1879,
    "reset_interval_s": 7200, "next_reset_s": 5321,
    "resets": 0, "reconnects": 0, "ended": null
  },
  "recording": {
    "active": true, "path": "/home/me/Music/KiwiSDR/20261026_net.wav",
    "seconds": 1820.4, "gap_seconds": 0.0, "waiting_for_audio": false,
    "max_seconds": 3600, "remaining_s": 1779
  },
  "audio_output": true
}
```

`state` is one of:

| `state` | Meaning |
|---|---|
| `idle` | not connected |
| `connecting` | first connection attempt in progress |
| `connected` | audio is flowing |
| `reconnecting` | the link dropped, retrying every 5 s |
| `busy` | all of the server's channels are taken, retrying every 15 s |
| `failed` | gave up; `last_error` says why (bad password, server down, redirect loop) |

`session` is `null` before the first connect.

### `POST /api/v1/connect`
| Field | Type | Default | |
|---|---|---|---|
| `server` | string | **required** | `host`, `host:port` or a URL; the port defaults to 8073 |
| `password` | string | `""` | the Kiwi's user password, or its time-limit-exemption password |
| `user` | string | `"kiwiremote"` | name shown in the Kiwi's user list |
| `wait` | number | `0` | seconds (at most 60) to wait for a definite answer |
| `session_limit_min` | number | from policy | override for this session; `0` = no limit |
| `reset_interval_min` | number | from policy | override for this session; `0` = no reset |
| `compression` | bool | from GUI/CLI | request ADPCM-compressed audio |

If a connection already exists, it is replaced. A running recording continues across the switch, and if the new server's sample rate differs, a new file part (`_1.wav`) is started.

- **Without `wait`**, the call returns `202 Accepted` immediately, and the app keeps trying in the background.
- **With `wait`**, the call returns one of:
  - `200`: connected
  - `503 busy`: all channels taken; the app keeps retrying
  - `502 connect_failed`: bad password or server down; the app has stopped trying
  - `504 timeout`: no answer yet; the app is still trying

### `POST /api/v1/disconnect`
Disconnects and ends the session. **A running recording is closed as well.** The response includes `recording_stopped`, which holds the closed recording's info, or `null` if nothing was recording.

### `POST /api/v1/tune`
Any combination of `freq_hz` or `freq_khz` (number), `mode` (`USB` / `LSB` / `AM`; the aliases `PKTUSB`, `PKTLSB` and `SAM` are also accepted) and `passband` (Hz; `0` = the mode's default). Tuning works with or without a connection; the setting is applied on connect. Returns `{"tune": {...}}`.

### `POST /api/v1/record/start`
| Field | Type | |
|---|---|---|
| `path` | string | full file path. It must end in `.wav`, or have no extension, in which case `.wav` is added |
| `dir` | string | folder; the default is the GUI's recording folder or `--record-dir` |
| `name` | string | file name template, used when `path` isn't given. Fields: `{date}` (YYYYMMDD, UTC), `{time}` (HHMMSSZ), `{freq_khz}`, `{mode}`, `{server}`. Default `kiwi_{date}T{time}_{freq_khz}kHz_{mode}` |
| `max_seconds` | number | stop automatically after this much wall-clock time. Recommended, so a recording ends even if the scheduler never sends stop |

How file names are handled:
- Existing files are never overwritten: `_1`, `_2`, … is appended instead.
- Characters other than letters, digits and `._+-` in a name are replaced with `_`.
- The response gives the actual path.

When no Kiwi is connected, recording waits for audio. If audio stops mid-recording (a periodic reset or a reconnect), **silence is inserted for the gap** when audio returns, so a position in the file still matches the clock time. `gap_seconds` reports how much silence was inserted.

The API only records WAV (16-bit mono at the Kiwi's native ~12 kHz rate). The GUI can also record M4A.

### `POST /api/v1/record/stop`
Returns `{"was_recording": bool, "recording": {"active": false, "path", "seconds", "gap_seconds", "reason"}}`. If nothing was recording, `was_recording` is `false` and the call still returns `ok`, so it is safe to call unconditionally.

## Session policy

The policy is configured in the GUI box, or with headless flags. Overrides are per connect call.

| Rule | Default | |
|---|---|---|
| Limit with a password | 120 min | total connected time; then the recording is closed and the app disconnects |
| Limit without a password | 45 min | same |
| Periodic reset | every 120 min | disconnect, wait 15 s (`reset_pause_s`), reconnect. The limit keeps counting across resets |
| Local exemption | on | Kiwis reached over loopback, private (10/8, 172.16/12, 192.168/16, fc00::/7) or link-local addresses get no limit and no reset |

- Locality is judged by the address actually connected to, not the name typed in. A LAN Kiwi reached through a public DNS name and your router's port forward counts as remote.
- `session_limit_min` and `reset_interval_min` passed to `/connect` override the policy, including for local Kiwis.
- With the defaults, a passworded session reaches its 120 min limit at the same moment the reset would fire, and the limit wins. Resets therefore only matter when a limit is longer than the reset interval, or turned off.

## Security

The API is designed to be reachable only from this computer:

- It binds to `127.0.0.1`. In headless mode, binding to any other address (`--api-host`) is refused unless a token is set.
- With a token configured, every request needs `Authorization: Bearer <token>` (or `X-Api-Token: <token>`).
- POST requests must be `application/json`. A web page can't send that to another site without a CORS preflight, and the API never approves one, so a page in your browser can't drive the app.
- The `Host` header must be a loopback name. This blocks DNS-rebinding attacks.
- Kiwi passwords are never logged and never returned by `/status`, which only reports `"password": true/false`.

## Command-line client

```bash
python run_kiwiremote.py ctl status
python run_kiwiremote.py ctl connect kiwi.example.net:8073 --password secret --wait 10
python run_kiwiremote.py ctl tune --khz 7110 --mode LSB
python run_kiwiremote.py ctl record-start --name "{date}_texas_net" --max-seconds 5400
python run_kiwiremote.py ctl record-stop
python run_kiwiremote.py ctl disconnect
```

The client takes `--host`, `--port` and `--token`; the token can also come from `$KIWIREMOTE_API_TOKEN`. It prints the JSON response and exits with `0` (ok), `1` (the API reported an error) or `2` (the app couldn't be reached). Frozen builds include the same client as `kiwiremote-cli ctl …`.

## Using it from Python

`kiwiremote/ctl.py` contains `ApiClient`, which depends only on the standard library and can be copied into another project:

```python
from kiwiremote.ctl import ApiClient

kiwi = ApiClient()                                  # 127.0.0.1:7373, token=None
code, r = kiwi.connect("kiwi.example.net:8073", password="pw", wait=15)
if not r["ok"]:
    raise RuntimeError(r["error"]["message"])
kiwi.tune(freq_khz=7110, mode="LSB")                 # skip if fldigi tunes via rigctld
code, r = kiwi.record_start(name="{date}_texas_net", max_seconds=90 * 60)
print("recording to", r["recording"]["path"])
# ... at the end of the net:
kiwi.record_stop()
kiwi.disconnect()
```

Equivalent `curl` commands:

```bash
curl -s localhost:7373/api/v1/status
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"server":"kiwi.example.net:8073","password":"pw","wait":15}' localhost:7373/api/v1/connect
```

## Headless mode

```bash
python run_kiwiremote.py --headless --record-dir ~/net-recordings
python run_kiwiremote.py --headless --help        # all options
```

Headless mode runs the API, rigctld and recording without a window and without Qt. It suits machines with no desktop session or no sound card. Useful options:

- `--api-port`, `--api-token`
- `--rigctl-port` or `--no-rigctl`
- `--audio-device TEXT`: also play to an output device whose name contains `TEXT`
- `--connect HOST[:PORT] --password …`: connect at startup
- the policy flags: `--limit-password-min`, `--limit-public-min`, `--reset-interval-min`, `--reset-pause-s`, `--no-local-exempt`

Ctrl+C or SIGTERM (Ctrl+Break on Windows) shuts down cleanly and finalizes the WAV file. Killing the process with `kill -9` or Task Manager leaves the WAV header without its final length.
