# PyInstaller spec:  pyinstaller kiwiremote.spec
# Produces dist/KiwiSDR Remote/ (Windows, Linux) or dist/KiwiSDR Remote.app (macOS) with two
# executables sharing one set of libraries:
#   "KiwiSDR Remote"   windowed GUI
#   "kiwiremote-cli"   console program for --headless and 'ctl' (on Windows the GUI exe has no console)
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

KC = os.path.join("third_party", "kiwiclient")
if not os.path.isfile(os.path.join(KC, "kiwi", "client.py")):
    raise SystemExit("Run 'python fetch_kiwiclient.py' first")
sys.path.insert(0, KC)

hidden = (["kiwi", "kiwi.client", "kiwi.worker", "kiwi.rigctld", "kiwi.wavreader",
           "kiwi.wsclient", "chunk"]
          + collect_submodules("mod_pywebsocket") + collect_submodules("kiwiremote"))
for optional in ("samplerate", "av"):
    try:
        __import__(optional)
        hidden.append(optional)
    except ImportError:
        print("NOTE: optional package %r not installed; build will lack it" % optional)

a = Analysis(["run_kiwiremote.py"], pathex=[".", KC], hiddenimports=hidden,
             excludes=["tkinter"], noarchive=False)
pyz = PYZ(a.pure)
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name="KiwiSDR Remote",
          console=False, upx=False)
cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="kiwiremote-cli",
          console=True, upx=False)
coll = COLLECT(gui, cli, a.binaries, a.datas, name="KiwiSDR Remote", upx=False)
if sys.platform == "darwin":
    app = BUNDLE(coll, name="KiwiSDR Remote.app", bundle_identifier="net.kiwiremote.app",
                 info_plist={"NSHighResolutionCapable": True})
