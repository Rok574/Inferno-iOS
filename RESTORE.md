# Restoring a guest, without a companion VM

ChefKiss's setup needs a second virtual machine: the emulated iPhone's USB port goes
to a socket, and a Linux VM running `usbmuxd` sits on the other end to play the USB
host. That VM does nothing else. [`netlab/muxd.py`](netlab/muxd.py) replaces it — it
is the USB host, speaks the mux protocol to the guest, and serves usbmuxd's own
protocol to the ordinary libimobiledevice tools. So a restore needs one machine, and
`idevicerestore` talks to the emulator directly.

This page is the whole path from an empty folder to a booting guest: which files you
need, where each one comes from, and the exact commands. Nothing here is automated by
the app, and nothing here is distributed with it — you build your own set from your
own IPSW, which is also what [ChefKiss asks](https://chefkiss.dev/guides/inferno/file-setup/).

> **Read ChefKiss's [Setup guide](https://chefkiss.dev/guides/inferno/) first.** This
> page follows it and only replaces the companion VM. Their guide is the source of
> truth for everything else; do not share images, firmware or keys you produce, and
> do not automate any of it.

---

## What you need

| Thing | Where it comes from |
|---|---|
| An IPSW for iPhone 11 (`iPhone12,1`) | Apple's own CDN; find the link on [ipsw.me](https://ipsw.me/iPhone12,1) or the [`api.ipsw.me`](https://api.ipsw.me/v4/device/iPhone12,1?type=ipsw) listing |
| SEP ROM `AppleSEPROM-Cebu-B1` | [securerom.fun](https://securerom.fun) |
| The SEP firmware key for **your** build | [The Apple Wiki](https://theapplewiki.com), page `Keys:<codename> <build> (iPhone12,1)` |
| `ticket.shsh2` and the two ticket scripts | [ChefKiss Extras](https://chefkiss.dev/guides/inferno/file-setup/) — the guide links `create_apticket.py`, `create_septicket.py` and `ticket.shsh2` |
| `img4` | [xerub/img4lib](https://github.com/xerub/img4lib) |
| The emulator | [MakrSas/Inferno, branch `ios`](https://github.com/MakrSas/Inferno/tree/ios) — a fork of [ChefKissInc/Inferno](https://github.com/ChefKissInc/Inferno) |
| `idevicerestore` and its libraries | [libimobiledevice](https://github.com/libimobiledevice/idevicerestore) — build from git, the releases are too old |

Tools on a Mac: `brew install meson ninja pkgconf libzip lzfse openssl qemu ldid`,
plus Xcode's command line tools. Python needs `pyasn1` and `pyasn1-modules`.

Which iOS version to pick is a separate question — see
[`RESTORE-FINDINGS.md`](RESTORE-FINDINGS.md) for what is known to work. **iOS 14.0
beta 5 (18A5351d) is the version the guide uses and the one this app is built
around.**

---

## 1. The folder and the disks

Everything lives in one directory. The commands below assume it is the current one
and that `qemu-img` comes from Homebrew's QEMU.

```bash
mkdir -p InfernoData && cd InfernoData
qemu-img create -f raw root      32G
qemu-img create -f raw firmware   8M
qemu-img create -f raw syscfg   128K
qemu-img create -f raw ctrl_bits  8K
qemu-img create -f raw nvram      8K
qemu-img create -f raw effaceable 4K
qemu-img create -f raw panic_log  1M
qemu-img create -f raw sep_nvram 64K
qemu-img create -f raw sep_ssc  128K
```

`root` is 32 GB of holes and costs nothing until the restore fills it.

## 2. The IPSW

Download it yourself. To find the link for a version:

```bash
curl -s "https://api.ipsw.me/v4/device/iPhone12,1?type=ipsw" |
  python3 -c "import json,sys;[print(f['version'], f['buildid'], f['url']) for f in json.load(sys.stdin)['firmwares']]"
```

Unpack everything except the big images — the emulator never loads them, and
`idevicerestore` reads them from the `.ipsw` itself:

```bash
unzip -q -o <ipsw> BuildManifest.plist -d Restore
SKIP=($(python3 - Restore/BuildManifest.plist <<'PY'
import plistlib, sys
m = plistlib.load(open(sys.argv[1], "rb"))
skip = {e["Info"]["Path"] for i in m["BuildIdentities"] for c, e in i["Manifest"].items()
        if (c == "OS" or c.startswith("Cryptex1,")) and e.get("Info", {}).get("Path", "").endswith(".dmg")}
print(" ".join(sorted(skip)))
PY
))
unzip -q -o <ipsw> -x "${SKIP[@]}" -d Restore
```

## 3. The tickets

The version is not signed by Apple, so the ticket is forged. Both scripts and the
`ticket.shsh2` they take come from ChefKiss's File Setup page.

```bash
python3 create_apticket.py  n104ap Restore/BuildManifest.plist ticket.shsh2 root_ticket.der
python3 create_septicket.py n104ap Restore/BuildManifest.plist ticket.shsh2 sep_root_ticket.der
```

Keep `root_ticket.der`. It is needed for every boot afterwards, not just the restore.

## 4. The SEP firmware

Look up `SEPFirmwareIV` and `SEPFirmwareKey` on the Apple Wiki page for **your exact
build** (they differ per build and per device), and concatenate them as `IVKEY`:

```bash
SEP=Restore/Firmware/all_flash/sep-firmware.n104.RELEASE.im4p
VERSION="$(img4 -v -i "$SEP" -o sep-firmware.n104.RELEASE -k "$IVKEY" | tail -n 1)"
echo "$VERSION"          # must print: none
img4 -A -F -o sep-firmware.n104.RELEASE.new.img4 -i sep-firmware.n104.RELEASE \
     -M sep_root_ticket.der -T rsep -V "$VERSION"
```

> On iOS 16 and newer `img4 -v` prints a dump of the payload's extra properties
> before the version, which is why only the **last line** is taken. On iOS 14 the
> whole output is already just `none`.

## 5. Build the emulator

Follow [ChefKiss's Host Setup](https://chefkiss.dev/guides/inferno/host-setup/), or
build this fork the same way. The rig scripts in `netlab/` expect the result in
`inferno-src/build-macos/qemu-system-aarch64`.

## 6. Restore, with no second VM

Two terminals. **`muxd.py` has to be listening before the emulator starts** — the
emulator dials the socket once, at start-up, and never again.

```bash
# terminal 1: the USB host and the usbmuxd protocol
netlab/muxd.py --usb /tmp/iusb.sock --socket /tmp/inferno-usbmuxd
```

```bash
# terminal 2: the machine, booted from the erase ramdisk
DATA=<your folder> ACCEL=tcg netlab/lab16.sh restore
```

`lab16.sh` reads the kernel, device tree, ramdisk and trustcache out of
`BuildManifest.plist`, so it works for any build. `ACCEL=hvf` is much faster where
the guest's kernel allows it (iOS 14 does; see the findings for what does not).

Wait until `muxd` prints `mux version 2.0` and the guest's console says
`waiting for host to trigger start of restore`. Then, in a third terminal:

```bash
USBMUXD_SOCKET_ADDRESS=UNIX:/tmp/inferno-usbmuxd \
  idevicerestore --erase --restore-mode -y -i 0x1122334455667788 \
                 -T <your folder>/InfernoData/root_ticket.der <ipsw>
```

With `-T` the ticket comes from the file and **nothing contacts Apple's signing
servers**, which is also what makes an offline restore possible.

The machine turns itself off when the first stage finishes: the emulator notices the
completed restore and says so.

## 7. The filesystem patches

Software rendering needs the dyld shared cache patched, and a few launch daemons have
to be switched off. Both are ChefKiss's
[Filesystem Patches](https://chefkiss.dev/guides/inferno/fs-patches/) step, with
their own patcher from
[git.chefkiss.dev/AppleHax/InfernoFSPatcher](https://git.chefkiss.dev/AppleHax/InfernoFSPatcher).
On a Mac the disk is attached with `hdiutil`, as their page describes.

## 8. The jailbreak bootstrap

A bare guest has no shell, and half of what this app does talks to one. ChefKiss's
[Jailbreak Utility Bootstrap](https://chefkiss.dev/guides/inferno-post-setup/jailbreak-bootstrap/)
puts checkra1n's `core_bootstrap` on the image and adds a `bash` launch daemon on
`/dev/console`; that bootstrap covers **iOS 12 to 14**.

For a newer guest the bootstrap is a different one — rootful Procursus, the same set
palera1n installs (`https://static.palera.in/bootstrap-1900.tar.zst` for iOS 16),
with Sileo or Zebra from `https://strap.palera.in`. The official Procursus bootstrap
of the same name is rootless (everything under `/var/jb`).

## 9. Boot it

```bash
DATA=<your folder> netlab/lab16.sh boot
```

Or copy the folder onto a phone and use the app — see the README for the layout it
expects.

---

## On the phone, with no computer at all

Every piece above is portable: the tickets are Python and DER, the SEP firmware is
one `img4` call, the restore is `idevicerestore` over a USB host that this app
already contains for its networking. The remaining work is in the app, and it is
tracked in [`TODO.md`](TODO.md). What already works there:

- the emulator itself, with the same USB socket the rig uses;
- an in-app USB host (the networking one), so no companion is needed on the phone
  either.

The status of the restore itself — and the one thing that currently stops a newer iOS
from being restored at all — is in [`RESTORE-FINDINGS.md`](RESTORE-FINDINGS.md).
