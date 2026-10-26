"""Turn unittest failures into GitHub annotations, so they are visible on the
PR / run page without downloading logs."""
import re
import sys

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()
blocks = re.split(r"^={50,}\n", text, flags=re.M)[1:]
if not blocks:
    tail = "\n".join(text.splitlines()[-40:])
    print("::error title=tests failed::" + tail.replace("%", "%25").replace("\n", "%0A"))
for b in blocks:
    b = b.split("\n" + "-" * 70 + "\nRan ")[0].strip()
    title = b.splitlines()[0][:200]
    body = b[-6000:].replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print("::error title=%s::%s" % (title.replace(":", " -").replace(",", ";"), body))
