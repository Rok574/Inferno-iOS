#!/bin/bash
# Runs an iOS 16 guest on the Mac, restored without a companion VM.
#
#   lab16.sh restore   boot the erase ramdisk, for idevicerestore
#   lab16.sh boot      boot the restored system
#
# The USB port goes to netlab/muxd.py, which has to be listening first; see
# `restore` below for the idevicerestore command. Everything the machine loads is
# looked up in the IPSW's own BuildManifest.plist rather than written in here, so
# the same script serves any build that Inferno supports.
#
#   DATA   the data folder: InfernoData/ with Restore/ extracted from the IPSW,
#          the disks, root_ticket.der and the SEP firmware (default ~/inferno-ios/ios16)
#   ACCEL  hvf (default here: a restore under TCG takes hours) or tcg
#   MEM    guest memory, 4G by default; the phone gives the guest 2G
#   SMP    cores, 4 by default -- one of them is the SEP, so that is three
#          application cores; the real part has six plus the SEP
#   GUI    sdl to open a window; none by default
#   BOOTARGS  extra boot arguments, appended to the fixed ones
set -uo pipefail

MODE="${1:-}"
case "$MODE" in restore|boot) ;; *) echo "usage: $0 restore|boot" >&2; exit 2 ;; esac

LAB="$(cd "$(dirname "$0")" && pwd)"
SRC="$LAB/../inferno-src"
QEMU="$SRC/build-macos/qemu-system-aarch64"
DATA="${DATA:-$HOME/inferno-ios/ios16}"
D="$DATA/InfernoData"
SOCK="${SOCK:-/tmp/iusb.sock}"
QMP="${QMP:-/tmp/inf-lab16.qmp}"
GLOG="${GLOG:-$DATA/guest-$MODE.log}"

[ -x "$QEMU" ] || { echo "No emulator at $QEMU" >&2; exit 1; }
[ -f "$D/Restore/BuildManifest.plist" ] || { echo "No $D/Restore/BuildManifest.plist" >&2; exit 1; }
[ -S "$SOCK" ] || { echo "Start netlab/muxd.py first: nothing listens on $SOCK" >&2; exit 1; }

# The erase identity for the iPhone 11 names the files both modes need.
manifest() {
    python3 - "$D/Restore/BuildManifest.plist" "$1" <<'PY'
import plistlib, sys
manifest = plistlib.load(open(sys.argv[1], "rb"))
want = sys.argv[2]
for identity in manifest["BuildIdentities"]:
    info = identity.get("Info", {})
    if info.get("DeviceClass", "").lower() != "n104ap" or "Erase" not in info.get("Variant", ""):
        continue
    entry = identity["Manifest"].get(want)
    if entry and entry.get("Info", {}).get("Path"):
        print(entry["Info"]["Path"])
        sys.exit(0)
sys.exit(1)
PY
}

KERNEL="$(manifest KernelCache)"
DTB="$(manifest DeviceTree)"
RAMDISK="$(manifest RestoreRamDisk)"
# The ramdisk's own trustcache, as the stock guide passes for both modes; the
# emulator's kernel patches accept every binary in any case.
TRUSTCACHE="$(manifest RestoreTrustCache || echo "Firmware/$(basename "$RAMDISK").trustcache")"
for f in "$KERNEL" "$DTB" "$RAMDISK" "$TRUSTCACHE"; do
    [ -f "$D/Restore/$f" ] || { echo "Missing Restore/$f" >&2; exit 1; }
done
SEPFW="$D/sep-firmware.n104.RELEASE.new.img4"
for f in "$D/root_ticket.der" "$SEPFW" "$DATA/AppleSEPROM-Cebu-B1"; do
    [ -f "$f" ] || { echo "Missing $f" >&2; exit 1; }
done

# Expanded below as ${INITRD[@]+...}: macOS ships bash 3.2, where an empty
# array under `set -u` is an unbound variable, and `boot` passes no ramdisk.
INITRD=()
BOOTMODE=""
if [ "$MODE" = restore ]; then
    # INITRD_FILE overrides the ramdisk, e.g. a copy with libimg4.dylib patched
    # so seal_system_volume accepts a forged ticket on iOS 16.
    INITRD=(-initrd "${INITRD_FILE:-$D/Restore/$RAMDISK}")
    # Said outright, not left to NVRAM. On `auto-boot=true` -- which a kit made
    # from scratch carries -- the machine ignores the ramdisk, boots as usual,
    # finds no system, and panics a minute later in IOAESAccelerator.
    BOOTMODE=",boot-mode=enter_recovery"
else
    BOOTMODE=",boot-mode=exit_recovery"
fi

case "${ACCEL:-hvf}" in
    hvf) ACCEL_ARG="hvf" ;;
    *)   ACCEL_ARG="tcg,thread=${THREAD:-multi},tb-size=${TB:-128}" ;;
esac

ns() { # ns <file> <nsid> <nstype>
    echo "-drive file=$D/$1,format=raw,if=none,id=$1 -device nvme-ns,drive=$1,bus=nvme-bus.0,nsid=$2,nstype=$3,logical_block_size=4096,physical_block_size=4096"
}

echo "kernel $KERNEL, device tree $DTB, ramdisk $RAMDISK, trustcache $TRUSTCACHE"
rm -f "$QMP"
# shellcheck disable=SC2046
"$QEMU" \
  -L "$SRC/build-macos/qemu-bundle/opt/homebrew/share/qemu" -L /opt/homebrew/share/qemu \
  -accel "$ACCEL_ARG" \
  -M "t8030,usb-uplink-type=inferno,usb-uplink-addr=unix:$SOCK,trustcache=$D/Restore/$TRUSTCACHE,ticket=$D/root_ticket.der,sep-fw=$SEPFW,sep-rom=$DATA/AppleSEPROM-Cebu-B1,kaslr-off=true$BOOTMODE" \
  -kernel "$D/Restore/$KERNEL" \
  -dtb "$D/Restore/$DTB" \
  ${INITRD[@]+"${INITRD[@]}"} \
  -append "tlto_us=-1 agm-genuine=1 agm-authentic=1 agm-trusted=1 serial=3 wdt=-1 launchd_unsecure_cache=1 -vm_compressor_wk_sw ${BOOTARGS:-}" \
  -smp "${SMP:-4}" -m "${MEM:-4G}" \
  -chardev "socket,id=serial0,host=127.0.0.1,port=4555,server=on,wait=off,logfile=$GLOG,logappend=off" \
  -serial chardev:serial0 \
  -qmp "unix:$QMP,server,nowait" \
  -display "${GUI:-none}" \
  -audiodev none,id=quiet -global driver=apple.mca,property=audiodev,value=quiet \
  -drive "file=$D/sep_nvram,if=pflash,format=raw" \
  -drive "file=$D/sep_ssc,if=pflash,format=raw" \
  $(ns root 1 1) $(ns firmware 2 2) $(ns syscfg 3 3) $(ns ctrl_bits 4 4) \
  -drive "file=$D/nvram,if=none,format=raw,id=nvram" \
  -device "apple-nvram,drive=nvram,bus=nvme-bus.0,nsid=5,nstype=5,id=nvram,logical_block_size=4096,physical_block_size=4096" \
  $(ns effaceable 6 6) $(ns panic_log 7 8) \
  ${EXTRA:+$EXTRA} \
  > "$DATA/qemu-$MODE.log" 2>&1 &
echo "running, pid=$!; console $GLOG, emulator log $DATA/qemu-$MODE.log, QMP $QMP"

if [ "$MODE" = restore ]; then
    # iOS 16+ asks the host to sign the Cryptex1 tickets mid-restore; netlab/tssd.py
    # stands in for Apple's signing server. Start it alongside muxd (it needs the
    # build manifest and any Cryptex1 IM4M as a shape donor) and point
    # idevicerestore at it with --server. iOS 14 does not need it.
    cat <<EOF

When muxd reports the device, restore with:
  # iOS 16+ only: start the Cryptex1 signing stand-in first
  netlab/tssd.py --manifest "$D/Restore/BuildManifest.plist" \\
    --template "$DATA/cryptex_template.im4m" --port 8888 &

  USBMUXD_SOCKET_ADDRESS=UNIX:/tmp/inferno-usbmuxd \\
    ~/inferno-ios/tools/idevicerestore/src/idevicerestore --erase --restore-mode \\
    -i 0x1122334455667788 -T "$D/root_ticket.der" \\
    --server http://127.0.0.1:8888 <path to the .ipsw>
EOF
fi
