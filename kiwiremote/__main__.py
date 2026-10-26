# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point.

    python -m kiwiremote [--debug]              GUI
    python -m kiwiremote --headless [options]   no window: API + rigctld + recording
    python -m kiwiremote ctl <command> ...      control a running instance via the API
"""

import argparse
import logging
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["ctl"]:
        from kiwiremote.ctl import main as ctl_main
        return ctl_main(argv[1:])
    logging.basicConfig(level=logging.DEBUG if "--debug" in argv else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if "--headless" in argv:
        from kiwiremote.headless import run as run_headless
        return run_headless(argv)
    ap = argparse.ArgumentParser(prog="kiwiremote", description="KiwiSDR remote receiver",
                                 epilog="Also: --headless (see --headless --help) and 'ctl --help'.")
    ap.add_argument("--debug", action="store_true", help="verbose logging (Kiwi protocol, rigctl, API)")
    args, qt_args = ap.parse_known_args(argv)
    try:
        from kiwiremote.gui import run
    except ImportError as e:
        sys.stderr.write("Missing dependency: %s\nInstall with:  pip install -r requirements.txt\n"
                         "(or run without a window: --headless)\n" % e)
        return 1
    return run([sys.argv[0]] + qt_args)


if __name__ == "__main__":
    sys.exit(main())
