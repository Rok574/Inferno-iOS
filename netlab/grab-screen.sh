#!/bin/bash
# QMP screendump the running guest -> PNG. Arg1 = output png.
set -uo pipefail
QMP=/tmp/inf-lab16.qmp; OUT="${1:-/tmp/setup.png}"; PPM="/tmp/_grab.ppm"
python3 - "$PPM" <<PY
import socket,time,sys
s=socket.socket(socket.AF_UNIX); s.settimeout(6)
s.connect("$QMP"); time.sleep(0.2); s.recv(65536)
s.sendall(b'{"execute":"qmp_capabilities"}\n'); time.sleep(0.2); s.recv(65536)
s.sendall(b'{"execute":"screendump","arguments":{"filename":"%s"}}\n'%sys.argv[1].encode()); time.sleep(0.5); print(s.recv(65536).decode(errors="replace")[:200])
s.close()
PY
env -i PATH=/usr/bin:/bin sips -s format png "$PPM" --out "$OUT" >/dev/null 2>&1 && echo "PNG: $OUT" || echo "convert failed"
