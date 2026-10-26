# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal hamlib rigctld (NET rigctl, hamlib model 2) server.

Lets WSJT-X, fldigi, JS8Call, GridTracker, Log4OM, `rigctl -m 2`, etc. read
and set the frequency and mode. It runs on its own thread and talks to the
radio only through RadioController.set_frequency()/set_mode(), so it works
whether or not the Kiwi is currently connected.

Supported commands (short and long forms, plain and '+' extended responses):
  f get_freq            F set_freq <Hz>
  m get_mode            M set_mode <mode> <passband>
  v get_vfo             V set_vfo <vfo>
  t get_ptt             T set_ptt <0|1>        (receive-only: accepted, ignored)
  s get_split_vfo       S set_split_vfo        (always split off)
  l get_level <LEVEL>   STRENGTH returns S-meter in dB relative to S9
  L set_level           accepted, ignored
  \\dump_state  \\chk_vfo  \\get_powerstat  \\get_lock_mode  q/Q
A leading VFO argument ('f VFOA', 'l VFOA STRENGTH') is accepted and ignored.
Anything else -> RPRT -11 (feature not available) and a log line.
"""

import logging
import socket
import socketserver
import threading

log = logging.getLogger(__name__)

RPRT_OK = "RPRT 0\n"
RPRT_EINVAL = "RPRT -1\n"
RPRT_ENAVAIL = "RPRT -11\n"

S9_DBM = -73.0

# short char -> (long name, takes VFO-optional args)
LONG = {
    "f": "get_freq", "F": "set_freq", "m": "get_mode", "M": "set_mode",
    "v": "get_vfo", "V": "set_vfo", "t": "get_ptt", "T": "set_ptt",
    "s": "get_split_vfo", "S": "set_split_vfo", "l": "get_level", "L": "set_level",
    "q": "quit", "Q": "quit",
}
SHORT = {v: k for k, v in LONG.items()}

# hamlib mode bit flags (rig.h): AM=0x1 CW=0x2 USB=0x4 LSB=0x8
MODE_BITS = "0xd"

# hamlib VFO names (rig_strvfo). Clients in VFO mode (fldigi, hamlib >= 4)
# put one in front of the arguments of most commands: 'f VFOA', 'l VFOA STRENGTH'.
VFO_NAMES = {"VFOA", "VFOB", "VFOC", "CURRVFO", "VFO", "MEM", "MAIN", "SUB", "TX", "RX",
             "MAINA", "MAINB", "MAINC", "SUBA", "SUBB", "SUBC", "NONE"}


def _is_vfo(tok):
    return tok.upper() in VFO_NAMES


def _dump_state(max_hz):
    # Format expected by hamlib netrigctl_open(); layout copied from the
    # proven kiwiclient rigctld emulation (Rik van Riel), modes trimmed.
    lines = [
        "0",                    # protocol version
        "2",                    # rig model (NET rigctl)
        "0",                    # ITU region
        "0.000000 %f %s -1 -1 0x1 0x1" % (float(max_hz), MODE_BITS),
        "0 0 0 0 0 0 0",        # end rx ranges
        "0 0 0 0 0 0 0",        # end tx ranges (none: receive only)
    ]
    for step in (1, 10, 100, 1000, 5000, 9000, 10000):
        lines.append("%s %d" % (MODE_BITS, step))
    lines.append("0 0")
    lines += ["0xc 2800", "0xc 2400", "0x1 9800", "0x1 6000", "0 0"]   # filters
    lines += ["0", "0", "0", "0"]   # max_rit max_xit max_ifshift announces
    lines += ["", ""]               # preamp, attenuator lists
    lines += ["0x0"] * 6            # get/set func, level, parm
    lines += ["vfo_ops=0x0", "ptt_type=0x0", "done"]
    return "\n".join(lines) + "\n"


class _Handler(socketserver.StreamRequestHandler):
    timeout = None

    def handle(self):
        srv = self.server.owner
        peer = "%s:%s" % self.client_address[:2]
        srv._client_count(+1, peer)
        try:
            for raw in self.rfile:
                try:
                    text = raw.decode("ascii", "replace").strip("\r\n")
                except Exception:
                    continue
                if not text.strip():
                    continue
                reply, close = srv.handle_line(text)
                if reply:
                    self.wfile.write(reply.encode("ascii", "replace"))
                    self.wfile.flush()
                if close:
                    break
        except (ConnectionError, OSError):
            pass
        finally:
            srv._client_count(-1, peer)


class _TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _TCPServer6(_TCPServer):
    address_family = socket.AF_INET6


class RigctldServer:
    def __init__(self, controller, host="127.0.0.1", port=4532, on_activity=None):
        self.controller = controller
        self.host = host
        self.port = int(port)
        self._on_activity = on_activity or (lambda kind, payload: None)
        self._server = None
        self._thread = None
        self._clients = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        cls = _TCPServer6 if ":" in self.host else _TCPServer
        self._server = cls((self.host, self.port), _Handler)
        self._server.owner = self
        self.port = self._server.server_address[1]   # resolves port 0 in tests
        self._thread = threading.Thread(target=self._server.serve_forever, name="rigctld", daemon=True)
        self._thread.start()
        log.info("rigctld listening on %s:%d", self.host, self.port)

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
            self._thread = None

    @property
    def running(self):
        return self._server is not None

    @property
    def clients(self):
        return self._clients

    def _client_count(self, delta, peer):
        with self._lock:
            self._clients += delta
            n = self._clients
        self._on_activity("clients", (n, peer, delta > 0))

    # ------------------------------------------------------------------ protocol
    def handle_line(self, line):
        """Process one input line (may hold several short commands). Returns (reply, close)."""
        line = line.strip()
        extended = False
        sep = "\n"
        if line[:1] in "+;|,":
            extended = True
            if line[0] in ";|,":
                sep = line[0]
            line = line[1:].strip()

        if line.startswith("\\"):
            parts = line[1:].split()
            cmds = [(parts[0], parts[1:])] if parts else []
        else:
            cmds = self._split_short(line)

        out = []
        close = False
        for name, args in cmds:
            long_name = LONG.get(name, name)
            if long_name == "quit":
                close = True
                break
            if long_name != "set_vfo" and args and _is_vfo(args[0]):
                args = args[1:]           # single VFO: the target VFO doesn't matter
            try:
                values, rc = self._dispatch(long_name, args)
            except Exception as e:
                log.debug("rigctl %s %s failed: %s", long_name, args, e)
                values, rc = [], -1
            if values is None:            # raw payload (dump_state, chk_vfo)
                out.append(rc)
            elif extended:
                hdr = "%s:" % long_name + (" " + " ".join(args) if args else "")
                body = [hdr] + ["%s: %s" % kv if isinstance(kv, tuple) else str(kv) for kv in values]
                body.append("RPRT %d" % rc)
                out.append(sep.join(body) + "\n")
            elif values:
                if rc != 0:
                    out.append("RPRT %d\n" % rc)
                else:
                    out.append("".join("%s\n" % (v[1] if isinstance(v, tuple) else v) for v in values))
            else:
                out.append("RPRT %d\n" % rc)
        return "".join(out), close

    @staticmethod
    def _split_short(line):
        """'F 7074000 M USB 2400 f' -> [('F',['7074000']), ('M',['USB','2400']), ('f',[])]"""
        tokens = line.split()
        cmds = []
        nargs = {"F": 1, "M": 2, "V": 1, "T": 1, "S": 2, "l": 1, "L": 2}
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if len(tok) == 1 and tok in LONG:
                n = nargs.get(tok, 0)
                # a leading VFO argument ('F VFOA 7074000', 'f VFOA') belongs to the
                # command; for 'V' the VFO is the argument itself
                if tok != "V" and i + 1 < len(tokens) and _is_vfo(tokens[i + 1]):
                    n += 1
                args = tokens[i + 1:i + 1 + n]
                cmds.append((tok, args))
                i += 1 + len(args)
            else:
                cmds.append((tok, tokens[i + 1:]))
                break
        return cmds

    def _dispatch(self, cmd, args):
        c = self.controller
        if cmd == "get_freq":
            return [("Frequency", "%d" % c.tune.freq_hz)], 0
        if cmd == "set_freq":
            hz = float(args[0])
            c.set_frequency(hz, source="rigctl")
            self._on_activity("command", "F %.0f" % hz)
            return [], 0
        if cmd == "get_mode":
            st = c.tune
            return [("Mode", st.mode), ("Passband", "%d" % st.passband)], 0
        if cmd == "set_mode":
            mode = args[0]
            pb = int(float(args[1])) if len(args) > 1 else 0
            if pb < 0:                    # RIG_PASSBAND_NOCHANGE
                pb = None
            try:
                c.set_mode(mode, pb, source="rigctl")
            except ValueError:
                log.info("rigctl: unsupported mode %s", mode)
                return [], -11
            self._on_activity("command", "M %s %s" % (mode, pb or ""))
            return [], 0
        if cmd == "get_vfo":
            return [("VFO", "VFOA")], 0
        if cmd in ("set_vfo", "set_ptt", "set_split_vfo", "set_level", "set_func", "set_parm",
                   "set_powerstat", "set_lock_mode", "set_rit", "set_xit"):
            return [], 0
        if cmd == "get_ptt":
            return [("PTT", "0")], 0
        if cmd == "get_split_vfo":
            return [("Split", "0"), ("TX VFO", "VFOA")], 0
        if cmd == "get_level":
            level = (args[0] if args else "").upper()
            if level == "STRENGTH":
                rssi = c.rssi
                return [("Level", "%d" % round((rssi if rssi is not None else -127.0) - S9_DBM))], 0
            return [("Level", "0")], 0
        if cmd == "dump_state":
            return None, _dump_state(c.freq_limits[1])
        if cmd == "chk_vfo":
            return None, "CHKVFO 0\n"     # same reply kiwiclientd uses with WSJT-X/fldigi
        if cmd == "get_powerstat":
            return [("Power Status", "1")], 0
        if cmd == "get_lock_mode":
            return [("Locked", "0")], 0
        if cmd in ("get_rit", "get_xit"):
            return [("RIT", "0")], 0
        if cmd == "get_info":
            return [("Info", "KiwiSDR Remote")], 0
        log.info("rigctl: unsupported command %r %s", cmd, args)
        return [], -11
