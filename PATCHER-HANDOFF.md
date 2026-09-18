# Post-restore patches without a computer

The restore itself runs on the phone now (see `RESTORE-HANDOFF.md`). What still
needs a Mac is what ChefKiss calls **Filesystem Patches**: the `dyld_shared_cache`
of the restored system has to be patched for software rendering, and the
jailbreak bootstrap has to be dropped onto the volume. This file is where that
work stands.

## The shape of the answer

Their patcher (`~/inferno-ios/tools/InfernoFSPatcher`, ~2000 lines of C++) works
on **one file**: `<DYLD_CACHE_PATH>`. It is a Mach-O analyser and assembler --
it finds functions and writes `movz`/`ret`/`nop` over them. No filesystem code at
all.

So the hard part was never the patch; it is reaching that file on the guest's
APFS volume from a phone. The plan is to let the guest do it: boot the machine on
a ramdisk of our own with a small program as its first process, which mounts the
restored volume (the ramdisk kernel has APFS), patches the cache in place, drops
the bootstrap, and powers off.

We cannot ship a ready-made image: it would carry Apple's binaries, which their
guide forbids distributing. The image has to be **built on the phone out of the
user's own ramdisk**.

## What is already proven, on the rig

Run it as:

    DATA=~/Downloads/InfernoKit ACCEL=tcg GUI=none MEM=2G \
      GLOG=/tmp/x.log INITRD_FILE=<our.dmg> netlab/lab16.sh restore

- A plain **8 MB case-sensitive HFS+ image** made with `hdiutil`, passed raw
  (not im4p-wrapped) as `-initrd`, is mounted by the kernel: the console says
  `BSD root: md0, major 3, minor 0`.
- The kernel then execs `/sbin/launchd` from it. Our file is found and started.
- **A static executable is refused.** Panic: `unexpected SIGKILL of init ...
  namespace 9 code 0x1` -- namespace 9 is exec, code 1 is BAD_MACHO. Tried, and
  none of it helped: arm64 and arm64e, `LC_BUILD_VERSION` written in by hand
  (the linker omits it under `-static`), the `MH_PIE` flag set, an ad-hoc
  signature. The stock `launchd` copied into the same image fails identically,
  which is what rules the binary out as the cause.
- **With `/usr/lib/dyld` in the image the error changes**, and that is the proof
  the path works: `initproc exited -- namespace 6 subcode 0x1, dyld cache load
  error: shared cache file open() failed / Library not loaded:
  /usr/lib/libSystem.B.dylib`. dyld runs; it wants its libraries.

So the first process must be an ordinary dynamic binary, and the image must carry
`dyld` plus the dylibs it pulls in.

## What the stock ramdisk is

`Restore/038-44135-124.dmg` is an im4p; unwrap it with
`~/inferno-ios/tools/img4lib/img4 -i <dmg> -o rd.dmg`, then `hdiutil attach`.

- Case-sensitive HFS+, volume `AzulSeed18A5351d.arm64eCustomerRamDisk`,
  **69 MB of content**.
- `/usr/lib/dyld` (748 KB) and individual dylibs -- **no shared cache**, which is
  why it is so small.
- `/sbin/launchd` (400 KB): arm64e, dynamic, PIE, `LC_BUILD_VERSION`.
- `/bin` has only `cat expr ln mkdir mv rm` -- no shell. `/usr/sbin` has `asr`,
  `hdik`, `mtree`, `nvram`, `syslogd`.
- Launch daemons live in `/System/Library/LaunchDaemons` (five of them), so
  adding one is a plist next to the others.

The emulator's kernel patches already let unsigned binaries run (`bypass code
signature checks`, `all binaries in trustcache`), so our program needs no real
signature.

## Second session: our code runs in the guest, the disk does not answer

Proven on the rig, with a 96 MB HFS+ image built by `hdiutil` out of the stock
ramdisk's own files plus ours:

- **A dynamic arm64e binary of ours runs as the first process.** It printed to
  `/dev/console` -- the console the emulator logs -- and reset the machine:
  `*** INFERNO INIT: our first process is alive ***`. So the earlier static
  failures really were about static linking, and nothing else stands in the way.
- **It also runs as an ordinary launch daemon**, which is better: replacing
  `launchd` means none of the stock daemons come up. A plist in
  `/System/Library/LaunchDaemons` with `RunAtLoad` is enough, and
  `StandardOutPath` of `/dev/console` puts its output in the guest log.
- `/System/Library/Filesystems/apfs.fs/apfs_boot_util` takes a phase number
  (`1` or `2`); both exit 0.
- **The restored volume cannot be reached from a bare ramdisk boot.** `/dev` holds
  only `md0`, `disk0`, `rdisk0`; no slices ever appear, `apfs_boot_util` changes
  nothing, and **`/dev/disk0` cannot even be opened**. The NVMe namespace of the
  system disk (nsid 1, nstype 1) is never made a block device: the guest's own
  log shows `Creating blockdevice` for nsid 2, 3, 6, 7 and 8 only.

That last point is what redirects the plan. The disk is live during a restore --
ASR writes to it -- so the patch should happen **at the end of the restore**,
inside the same ramdisk boot the app already performs, rather than in a separate
one afterwards. Our daemon sits in that ramdisk, waits for the volumes to appear
once `restored` has partitioned and written them, patches the cache, and lets the
machine power off as it already does.

If that turns out to be awkward, the fallback is to patch the filesystem image
**in flight**, as it is streamed over ASR: no mounting at all, but the app would
have to parse APFS read-only to find where the cache lives inside the image.

Useful while testing: `lab16.sh` refuses to start without a listener on
`/tmp/iusb.sock`; a python `AF_UNIX` socket that accepts and says nothing is
enough. And use a data folder whose `root` actually holds a system --
`stage/InfernoData/root` does (8.9 GB, GPT); `~/inferno-ios/ios14ab` does not
(16 KB, blank), which cost a run to notice.

## What is left to build

1. **Read HFS+** well enough to walk the stock ramdisk and pull files out.
2. **Write HFS+**: lay out a fresh volume with those files plus ours. Writing a
   new image is far easier than editing one in place -- nothing has to be
   allocated around existing data.
3. **The guest program**: dynamic, arm64e, built the way `build.sh` already
   builds `nsio` and the guest agent. Mount the system volume, patch, drop the
   bootstrap, power off.
4. **Port the patcher's logic** from InfernoFSPatcher, or compile it as-is --
   the app links C++ since the libyuv merge.
5. Wire it up: prepare the image, boot the machine on it once, then boot the
   restored system.

## Dead ends, so they are not tried twice

- A single freestanding file as the whole image. The kernel refuses static
  executables, whatever is done to the Mach-O.
- Shipping a prebuilt image inside the app: it would contain Apple's files.
