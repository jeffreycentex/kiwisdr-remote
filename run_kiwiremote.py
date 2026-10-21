#!/usr/bin/env python3
"""Launcher (also the PyInstaller entry point)."""
import sys

from kiwiremote.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
