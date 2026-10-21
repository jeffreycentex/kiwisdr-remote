"""Locate and import jks-prv/kiwiclient (not pip-installable).

Search order:
  1. $KIWICLIENT_DIR
  2. <project>/third_party/kiwiclient   (created by fetch_kiwiclient.py)
  3. sys._MEIPASS/kiwiclient            (PyInstaller bundle)
  4. already importable (on PYTHONPATH)
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_HERE)


def _candidates():
    env = os.environ.get("KIWICLIENT_DIR")
    if env:
        yield env
    yield os.path.join(_PROJECT, "third_party", "kiwiclient")
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield os.path.join(meipass, "kiwiclient")


def ensure_kiwiclient():
    for path in _candidates():
        if os.path.isfile(os.path.join(path, "kiwi", "client.py")):
            if path not in sys.path:
                sys.path.insert(0, path)
            break
    try:
        import kiwi.client  # noqa: F401
        import mod_pywebsocket  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "kiwiclient not found (%s). Run 'python fetch_kiwiclient.py' in the project "
            "folder, or set KIWICLIENT_DIR to a kiwiclient checkout." % e) from e


ensure_kiwiclient()

from kiwi.client import (  # noqa: E402
    KiwiSDRStream,
    KiwiError,
    KiwiTooBusyError,
    KiwiRedirectError,
    KiwiDownError,
    KiwiBadPasswordError,
    KiwiConnectionError,
    KiwiServerTerminatedConnection,
)
