"""Radio state model shared by the GUI, the rigctld server and the Kiwi worker.

Units: frequency is stored in Hz (hamlib convention). The Kiwi API uses kHz;
conversion happens only at the Kiwi boundary.
"""

from dataclasses import dataclass, replace

# Modes exposed in the UI. Key = hamlib/rigctl mode name, value = Kiwi mode name.
MODES = {
    "USB": "usb",
    "LSB": "lsb",
    "AM": "am",
}

# rigctl clients sometimes ask for modes we don't have; map them to the nearest.
MODE_ALIASES = {
    "PKTUSB": "USB",
    "PKTLSB": "LSB",
    "USBD": "USB",
    "LSBD": "LSB",
    "AMS": "AM",
    "SAM": "AM",
    "PKTAM": "AM",
}

# Default passband width (Hz) per mode, used when a client passes 0 or nothing.
DEFAULT_PASSBAND = {
    "USB": 2800,
    "LSB": 2800,
    "AM": 9800,
}

# Low edge of the SSB passband (Hz from carrier). 200 Hz keeps low FT8/JS8
# tones while rejecting most hum/carrier leakage.
SSB_LOW_EDGE = 200

MIN_PASSBAND = 100
MAX_PASSBAND = 12000

MIN_FREQ_HZ = 1_000            # 1 kHz
DEFAULT_MAX_FREQ_HZ = 32_000_000  # refined per server once connected


def normalize_mode(name):
    """Return a canonical UI mode name (USB/LSB/AM) or None if unsupported."""
    if name is None:
        return None
    m = name.strip().upper()
    m = MODE_ALIASES.get(m, m)
    return m if m in MODES else None


def clamp_passband(mode, width):
    """0 or negative means 'default for this mode'."""
    if width is None or width <= 0:
        return DEFAULT_PASSBAND[mode]
    return int(max(MIN_PASSBAND, min(MAX_PASSBAND, width)))


def passband_edges(mode, width):
    """Return (low_cut, high_cut) in Hz relative to the carrier, as the Kiwi wants."""
    width = clamp_passband(mode, width)
    if mode == "USB":
        return SSB_LOW_EDGE, SSB_LOW_EDGE + width
    if mode == "LSB":
        return -(SSB_LOW_EDGE + width), -SSB_LOW_EDGE
    if mode == "AM":
        half = width // 2
        return -half, half
    raise ValueError("unsupported mode %r" % mode)


@dataclass(frozen=True)
class TuneState:
    freq_hz: int = 7_074_000
    mode: str = "USB"
    passband: int = DEFAULT_PASSBAND["USB"]

    def with_freq(self, hz):
        return replace(self, freq_hz=int(round(hz)))

    def with_mode(self, mode, passband=None):
        mode = normalize_mode(mode)
        if mode is None:
            raise ValueError("unsupported mode")
        if passband is None or passband <= 0:
            pb = DEFAULT_PASSBAND[mode] if mode != self.mode else self.passband
        else:
            pb = clamp_passband(mode, passband)
        return replace(self, mode=mode, passband=pb)


@dataclass(frozen=True)
class ServerSpec:
    host: str
    port: int = 8073
    password: str = ""
    user: str = "kiwiremote"

    @staticmethod
    def parse(text, password="", user="kiwiremote"):
        """Accept 'host', 'host:port', 'http://host:port/' forms."""
        t = text.strip()
        for prefix in ("http://", "https://", "ws://", "wss://"):
            if t.lower().startswith(prefix):
                t = t[len(prefix):]
        t = t.split("/", 1)[0]
        if not t:
            raise ValueError("empty server address")
        if t.startswith("["):  # [ipv6]:port
            host, _, rest = t[1:].partition("]")
            port = int(rest[1:]) if rest.startswith(":") else 8073
        elif t.count(":") == 1:
            host, p = t.split(":")
            port = int(p)
        else:
            host, port = t, 8073
        if not (0 < port < 65536):
            raise ValueError("bad port %d" % port)
        return ServerSpec(host=host, port=port, password=password, user=user or "kiwiremote")

    def label(self):
        return "%s:%d" % (self.host, self.port)
