"""Entry point:  python -m kiwiremote [--debug]"""

import argparse
import logging
import sys


def main():
    ap = argparse.ArgumentParser(prog="kiwiremote", description="KiwiSDR remote receiver GUI")
    ap.add_argument("--debug", action="store_true", help="verbose logging (Kiwi protocol, rigctl)")
    args, qt_args = ap.parse_known_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        from kiwiremote.gui import run
    except ImportError as e:
        sys.stderr.write("Missing dependency: %s\nInstall with:  pip install -r requirements.txt\n" % e)
        return 1
    return run([sys.argv[0]] + qt_args)


if __name__ == "__main__":
    sys.exit(main())
