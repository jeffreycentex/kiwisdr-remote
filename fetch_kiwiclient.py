#!/usr/bin/env python3
"""Download jks-prv/kiwiclient into third_party/kiwiclient.

kiwiclient is not published on PyPI, so KiwiSDR Remote fetches it from GitHub.
Pinned to a tested commit; pass --latest to take master instead.

    python fetch_kiwiclient.py [--latest]
"""

import io
import os
import shutil
import sys
import urllib.request
import zipfile

REPO = "jks-prv/kiwiclient"
PINNED = "4eb733e6b6147f7fbeb97ced64cdac029b202d18"   # v1.11, 2026-08-03
KEEP = ("kiwi/", "mod_pywebsocket/", "chunk/", "README.md")

here = os.path.dirname(os.path.abspath(__file__))
dest = os.path.join(here, "third_party", "kiwiclient")


def main():
    ref = "master" if "--latest" in sys.argv else PINNED
    url = "https://codeload.github.com/%s/zip/%s" % (REPO, ref)
    print("Downloading %s ..." % url)
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    zf = zipfile.ZipFile(io.BytesIO(data))
    root = zf.namelist()[0].split("/")[0]
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)
    for name in zf.namelist():
        rel = name[len(root) + 1:]
        if not rel or not rel.startswith(KEEP):
            continue
        target = os.path.join(dest, rel)
        if name.endswith("/"):
            os.makedirs(target, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as f:
                f.write(zf.read(name))
    with open(os.path.join(dest, "REVISION"), "w") as f:
        f.write(ref + "\n")
    print("kiwiclient (%s) installed in %s" % (ref, dest))


if __name__ == "__main__":
    main()
