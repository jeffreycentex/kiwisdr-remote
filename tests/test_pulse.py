"""create_virtual_cable() issues the right pactl commands (pactl mocked)."""

import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kiwiremote import sinks  # noqa: E402


class FakePactl:
    def __init__(self, sinks_=(), sources=()):
        self.sinks = list(sinks_)
        self.sources = list(sources)
        self.loaded = []

    def run(self, argv, **kw):
        out = ""
        if argv[1:3] == ["list", "short"]:
            names = self.sinks if argv[3] == "sinks" else self.sources
            out = "".join("%d\t%s\tmodule-x.c\ts16le 2ch 48000Hz\tIDLE\n" % (i, n)
                          for i, n in enumerate(names))
        elif argv[1] == "load-module":
            self.loaded.append(argv[2:])
            args = dict(a.split("=", 1) for a in argv[3:])
            if argv[2] == "module-null-sink":
                self.sinks.append(args["sink_name"])
                self.sources.append(args["sink_name"] + ".monitor")
            elif argv[2] == "module-remap-source":
                self.sources.append(args["source_name"])
        return subprocess.CompletedProcess(argv, 0, out, "")


class TestVirtualCable(unittest.TestCase):
    def patched(self, fake):
        return mock.patch.multiple(
            sinks, subprocess=mock.Mock(run=fake.run, SubprocessError=subprocess.SubprocessError),
            shutil=mock.Mock(which=lambda n: "/usr/bin/pactl"),
            sys=mock.Mock(platform="linux"))

    def test_creates_sink_and_source(self):
        fake = FakePactl()
        with self.patched(fake):
            self.assertEqual(sinks.create_virtual_cable(), ("KiwiSDR", "KiwiSDR_RX"))
        self.assertEqual(fake.loaded[0], ["module-null-sink", "sink_name=KiwiSDR",
                                          "sink_properties=device.description=KiwiSDR"])
        self.assertEqual(fake.loaded[1], ["module-remap-source", "master=KiwiSDR.monitor",
                                          "source_name=KiwiSDR_RX",
                                          "source_properties=device.description=KiwiSDR-RX"])

    def test_adds_only_missing_source(self):
        fake = FakePactl(["KiwiSDR"], ["KiwiSDR.monitor"])
        with self.patched(fake):
            sinks.create_virtual_cable()
            self.assertEqual([m[0] for m in fake.loaded], ["module-remap-source"])
            sinks.create_virtual_cable()          # idempotent
        self.assertEqual(len(fake.loaded), 1)

    def test_pactl_failure_raises(self):
        fake = FakePactl()
        fail = lambda argv, **kw: (fake.run(argv) if argv[1] == "list"
                                   else subprocess.CompletedProcess(argv, 1, "", "Module initialization failed"))
        with mock.patch.multiple(sinks, subprocess=mock.Mock(run=fail, SubprocessError=subprocess.SubprocessError),
                                 shutil=mock.Mock(which=lambda n: "/usr/bin/pactl"),
                                 sys=mock.Mock(platform="linux")):
            with self.assertRaisesRegex(RuntimeError, "Module initialization failed"):
                sinks.create_virtual_cable()


if __name__ == "__main__":
    unittest.main()
