#!/bin/bash
# After a successful 16.3.1 restore: boot the installed system headless and grab
# the screen (Setup.app) via QMP screendump. No window on the user's display.
set -uo pipefail
DATA=~/inferno-ios/ios1631
R="$DATA/runs"; GLOG="$R/boot-guest.log"; QMP=/tmp/inf-lab16.qmp
SHOT="${1:-$R/setup.png}"
# 1) stop the restore emulator cleanly
if [ -S "$QMP" ]; then
  python3 - <<PY 2>/dev/null || true
import socket,time
s=socket.socket(socket.AF_UNIX); s.settimeout(4)
try:
    s.connect("$QMP"); time.sleep(0.2); s.recv(65536)
    s.sendall(b'{"execute":"qmp_capabilities"}\n'); time.sleep(0.2); s.recv(65536)
    s.sendall(b'{"execute":"quit"}\n'); time.sleep(0.3)
except Exception as e: print("quit err",e)
finally: s.close()
PY
fi
pkill -f qemu-system-aarch64 2>/dev/null || true; sleep 2
: > "$GLOG"
# 2) boot the restored system, headless
ACCEL=tcg TB=256 MEM=4G DATA="$DATA" GLOG="$GLOG" "$(dirname "$0")/lab16.sh" boot >/dev/null 2>&1
echo "booting restored system; console -> $GLOG"
