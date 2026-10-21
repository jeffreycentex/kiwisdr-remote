"""Tiny fake KiwiSDR websocket server for headless tests.

Speaks just enough of the Kiwi SND protocol: HTTP upgrade, a few MSG params,
then uncompressed big-endian int16 SND frames carrying a 1 kHz tone. Records
every text command the client sends.
"""

import base64
import hashlib
import socket
import struct
import threading
import time

import numpy as np
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _frame(payload, opcode=0x2):
    n = len(payload)
    if n < 126:
        hdr = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return hdr + payload


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("eof")
        buf += chunk
    return buf


def _read_frame(sock):
    b0, b1 = _read_exact(sock, 2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", _read_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", _read_exact(sock, 8))[0]
    mask = _read_exact(sock, 4) if b1 & 0x80 else b"\0\0\0\0"
    data = bytearray(_read_exact(sock, n))
    for i in range(n):
        data[i] ^= mask[i % 4]
    return b0 & 0x0F, bytes(data)


import kiwiremote.kiwi_import  # noqa: E402,F401  (puts kiwiclient on sys.path)
from kiwi.client import indexAdjustTable, stepSizeTable  # noqa: E402


class AdpcmEncoder:
    """IMA ADPCM encoder matching kiwiclient's decoder (low nibble first)."""

    def __init__(self):
        self.index = 0
        self.prev = 0

    def _code(self, sample):
        step = stepSizeTable[self.index]
        diff = int(sample) - self.prev
        code = 0
        if diff < 0:
            code = 8
            diff = -diff
        delta = step >> 3
        if diff >= step:
            code |= 4; diff -= step; delta += step
        if diff >= step >> 1:
            code |= 2; diff -= step >> 1; delta += step >> 1
        if diff >= step >> 2:
            code |= 1; delta += step >> 2
        self.prev = max(-32768, min(32767, self.prev - delta if code & 8 else self.prev + delta))
        self.index = max(0, min(len(stepSizeTable) - 1, self.index + indexAdjustTable[code]))
        return code

    def encode(self, samples):
        out = bytearray()
        for i in range(0, len(samples), 2):
            lo = self._code(samples[i])
            hi = self._code(samples[i + 1])
            out.append(lo | (hi << 4))
        return bytes(out)


class FakeKiwi:
    def __init__(self, sample_rate=12001.135, bandwidth_hz=30_000_000, busy=False):
        self.sample_rate = sample_rate
        self.bandwidth_hz = bandwidth_hz
        self.busy = busy
        self.commands = []
        self.connections = 0
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen()
        self.port = self._srv.getsockname()[1]
        self._stop = threading.Event()
        self._conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def stop(self):
        self._stop.set()
        for c in self._conns:
            try:
                c.close()
            except OSError:
                pass
        self._srv.close()

    def drop_clients(self):
        for c in self._conns:
            try:
                c.shutdown(socket.SHUT_RDWR)
                c.close()
            except OSError:
                pass

    def _accept(self):
        while not self._stop.is_set():
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            self._conns.append(c)
            self.connections += 1
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            req = b""
            while b"\r\n\r\n" not in req:
                req += c.recv(4096)
            key = [l.split(b":", 1)[1].strip() for l in req.split(b"\r\n")
                   if l.lower().startswith(b"sec-websocket-key")][0]
            accept = base64.b64encode(hashlib.sha1(key + GUID.encode()).digest())
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
            state = {"compress": True}
            threading.Thread(target=self._reader, args=(c, state), daemon=True).start()
            send = lambda s: c.sendall(_frame(b"MSG\0" + s.encode()))
            if self.busy:
                send("too_busy=4")
                time.sleep(0.5)
                return
            send("bandwidth=%d" % self.bandwidth_hz)
            send("version_maj=1 version_min=826")
            send("audio_rate=12000")
            send("sample_rate=%.3f" % self.sample_rate)
            seq = 0
            n = 512
            phase = 0.0
            t0 = time.monotonic()
            while not self._stop.is_set():
                t = (phase + np.arange(n)) / self.sample_rate
                tone = (8000 * np.sin(2 * np.pi * 1000 * t)).astype(">i2")
                phase += n
                smeter = int((-80 + 127) * 10)       # -80 dBm
                if state["compress"]:
                    enc = state.setdefault("enc", AdpcmEncoder())
                    payload, flags = enc.encode(tone.astype(int)), 0x10
                else:
                    payload, flags = tone.tobytes(), 0
                body = b"SND" + struct.pack("<BI", flags, seq) + struct.pack(">H", smeter) + payload
                c.sendall(_frame(body))
                seq += 1
                # pace in real time
                due = t0 + phase / self.sample_rate
                d = due - time.monotonic()
                if d > 0:
                    time.sleep(d)
        except (OSError, ConnectionError, IndexError):
            pass

    def _reader(self, c, state):
        try:
            while True:
                op, data = _read_frame(c)
                if op == 0x8:
                    return
                if op == 0x1:
                    cmd = data.decode("ascii", "replace")
                    self.commands.append(cmd)
                    if cmd.startswith("SET compression="):
                        state["compress"] = cmd.endswith("=1")
        except (OSError, ConnectionError):
            pass

    def mods(self):
        return [x for x in self.commands if x.startswith("SET mod=")]
