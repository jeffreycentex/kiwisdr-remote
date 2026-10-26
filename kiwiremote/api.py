# SPDX-License-Identifier: GPL-3.0-or-later
"""HTTP/JSON remote-control API (stdlib only).

All routes live under /api/v1 so a later, incompatible API can be added as
/api/v2 next to it. Requests and responses are JSON. Every response has
"ok": true|false; errors carry {"error": {"code", "message"}} with a stable
code. See docs/API.md.

Security model (local-only by default)
--------------------------------------
* Binds to 127.0.0.1. Binding to any other address requires a token.
* Optional token: "Authorization: Bearer <token>" (or "X-Api-Token").
* POST bodies must be Content-Type application/json. A web page can't send
  that cross-origin without a CORS preflight, which this server never
  approves, so a browser can't be used to drive the API (CSRF).
* The Host header must name a loopback host (or the configured bind
  address), which defeats DNS-rebinding attacks.
"""

import hmac
import ipaddress
import json
import logging
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .service import ApiError

log = logging.getLogger(__name__)

API_VERSION = 1
PREFIX = "/api/v1"
DEFAULT_PORT = 7373
MAX_BODY = 64 * 1024
MAX_WAIT = 60.0

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]"}


def _is_loopback(host):
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def _num(body, key, kind=float):
    v = body.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ApiError("bad_request", "%s must be a number" % key)
    return kind(v)


def _str(body, key):
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise ApiError("bad_request", "%s must be a string" % key)
    return v


class _Handler(BaseHTTPRequestHandler):
    server_version = "KiwiSDR-Remote/%s" % __version__
    protocol_version = "HTTP/1.1"

    # quiet default logging; route through logging at debug level
    def log_message(self, fmt, *args):
        log.debug("api %s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ plumbing
    def _send(self, status, payload):
        data = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, code, message):
        self._send(status, {"ok": False, "error": {"code": code, "message": message}})

    def _check_access(self):
        api = self.server.api
        host = (self.headers.get("Host") or "").strip()
        if host.startswith("[") and "]" in host:        # [::1]:7373
            hostname = host[:host.index("]") + 1]
        elif host.count(":") == 1:                       # 127.0.0.1:7373
            hostname = host.split(":")[0]
        else:
            hostname = host
        if not (_is_loopback(hostname) or hostname in api.extra_hosts):
            raise ApiError("forbidden_host", "Host header %r not allowed" % host, 403)
        if api.token:
            auth = self.headers.get("Authorization", "")
            given = auth[7:].strip() if auth.lower().startswith("bearer ") else self.headers.get("X-Api-Token", "")
            if not hmac.compare_digest(given.encode(), api.token.encode()):
                raise ApiError("unauthorized", "missing or wrong API token", 401)

    def _body(self):
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ApiError("too_large", "request body too large", 413)
        raw = self.rfile.read(length) if length else b""
        if ctype != "application/json":
            raise ApiError("unsupported_media_type", "POST bodies must be application/json", 415)
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw)
        except ValueError as e:
            raise ApiError("bad_json", "invalid JSON: %s" % e)
        if not isinstance(body, dict):
            raise ApiError("bad_json", "JSON body must be an object")
        return body

    def _dispatch(self, method):
        try:
            self._check_access()
            path = self.path.split("?", 1)[0].rstrip("/") or "/"
            routes = self.server.api.routes
            handler = routes.get((method, path))
            if handler is None:
                if any(p == path for _, p in routes):
                    raise ApiError("method_not_allowed", "%s not allowed on %s" % (method, path), 405)
                raise ApiError("not_found", "no such endpoint: %s" % path, 404)
            body = self._body() if method == "POST" else {}
            status, payload = handler(body)
            payload = dict(payload)
            payload.setdefault("ok", status < 400)
            self._send(status, payload)
        except ApiError as e:
            self._error(e.http_status, e.code, str(e))
        except Exception as e:                     # never kill the server thread
            log.exception("api handler failed")
            self._error(500, "internal_error", str(e))

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):                          # no CORS, ever
        self._error(405, "method_not_allowed", "CORS is not supported")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Server6(_Server):
    address_family = socket.AF_INET6


class ApiServer:
    def __init__(self, service, host="127.0.0.1", port=DEFAULT_PORT, token=None):
        self.service = service
        self.host = host
        self.port = int(port)
        self.token = token or None
        self.extra_hosts = set()
        if not _is_loopback(host):
            if not self.token:
                raise ValueError("an API token is required when binding to %s (not loopback)" % host)
            self.extra_hosts.add(host)
        self._httpd = None
        self._thread = None
        self.routes = {
            ("GET", PREFIX): self._r_info,
            ("GET", PREFIX + "/status"): self._r_status,
            ("POST", PREFIX + "/connect"): self._r_connect,
            ("POST", PREFIX + "/disconnect"): self._r_disconnect,
            ("POST", PREFIX + "/tune"): self._r_tune,
            ("POST", PREFIX + "/record/start"): self._r_record_start,
            ("POST", PREFIX + "/record/stop"): self._r_record_stop,
        }

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        cls = _Server6 if ":" in self.host else _Server
        self._httpd = cls((self.host, self.port), _Handler)
        self._httpd.api = self
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="kiwi-api", daemon=True)
        self._thread.start()
        log.info("API listening on http://%s:%d%s%s", self.host, self.port, PREFIX,
                 " (token required)" if self.token else "")

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def running(self):
        return self._httpd is not None

    @property
    def url(self):
        h = "[%s]" % self.host if ":" in self.host else self.host
        return "http://%s:%d%s" % (h, self.port, PREFIX)

    # ------------------------------------------------------------------ routes
    def _r_info(self, body):
        return 200, {"api": "kiwisdr-remote", "api_version": API_VERSION, "app_version": __version__,
                     "endpoints": sorted("%s %s" % k for k in self.routes)}

    def _r_status(self, body):
        return 200, self.service.status()

    def _r_connect(self, body):
        server = _str(body, "server")
        if not server:
            raise ApiError("bad_request", "'server' is required, e.g. \"kiwi.example.net:8073\"")
        wait = min(MAX_WAIT, max(0.0, _num(body, "wait") or 0.0))
        compression = body.get("compression")
        if compression is not None and not isinstance(compression, bool):
            raise ApiError("bad_request", "compression must be true or false")
        st = self.service.connect(server, password=_str(body, "password") or "",
                                  user=_str(body, "user"), wait=wait,
                                  session_limit_min=_num(body, "session_limit_min"),
                                  reset_interval_min=_num(body, "reset_interval_min"),
                                  compression=compression)
        if wait:
            if st["state"] == "connected":
                return 200, st
            if st["state"] == "busy":
                return 503, dict(st, ok=False, error={"code": "busy", "message": st["last_error"]})
            if st["state"] == "failed":
                return 502, dict(st, ok=False, error={"code": "connect_failed", "message": st["last_error"]})
            return 504, dict(st, ok=False, error={"code": "timeout",
                                                  "message": "not connected after %g s; still trying" % wait})
        return 202, st

    def _r_disconnect(self, body):
        return 200, self.service.disconnect(reason="api")

    def _r_tune(self, body):
        freq = _num(body, "freq_hz")
        khz = _num(body, "freq_khz")
        if freq is not None and khz is not None:
            raise ApiError("bad_request", "give freq_hz or freq_khz, not both")
        if khz is not None:
            freq = khz * 1000.0
        mode = _str(body, "mode")
        pb = _num(body, "passband", int)
        if freq is None and mode is None and pb is None:
            raise ApiError("bad_request", "nothing to change: give freq_hz/freq_khz, mode or passband")
        return 200, {"tune": self.service.tune(freq, mode, pb, source="api")}

    def _r_record_start(self, body):
        info = self.service.start_recording(path=_str(body, "path"), directory=_str(body, "dir"),
                                            name=_str(body, "name"), max_seconds=_num(body, "max_seconds"))
        return 200, {"recording": info}

    def _r_record_stop(self, body):
        info = self.service.stop_recording(reason="api")
        return 200, {"recording": info or {"active": False}, "was_recording": info is not None}
