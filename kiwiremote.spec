# PyInstaller spec:  pyinstaller kiwiremote.spec
# Produces dist/KiwiSDR Remote/ (Windows, Linux) or dist/KiwiSDR Remote.app (macOS).
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

KC = os.path.join("third_party", "kiwiclient")
if not os.path.isfile(os.path.join(KC, "kiwi", "client.py")):
    raise SystemExit("Run 'python fetch_kiwiclient.py' first")
sys.path.insert(0, KC)

hidden = ["kiwi", "kiwi.client", "kiwi.worker", "kiwi.rigctld", "kiwi.wavreader",
          "kiwi.wsclient", "chunk"] + collect_submodules("mod_pywebsocket")
for optional in ("samplerate", "av"):
    try:
        __import__(optional)
        hidden.append(optional)
    except ImportError:
        print("NOTE: optional package %r not installed; build will lack it" % optional)

a = Analysis(["run_kiwiremote.py"], pathex=[".", KC], hiddenimports=hidden,
             excludes=["tkinter"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="KiwiSDR Remote",
          console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="KiwiSDR Remote", upx=False)
if sys.platform == "darwin":
    app = BUNDLE(coll, name="KiwiSDR Remote.app", bundle_identifier="net.kiwiremote.app",
                 info_plist={"NSHighResolutionCapable": True})
