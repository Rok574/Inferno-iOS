#!/usr/bin/env python3
"""A stand-in signing server for restoring iOS 16+ without Apple's TSS.

From iOS 16 on, the OS ships in Cryptex1 disk images (the system and app
"cryptexes"), and each needs its own ticket. Unlike the AP ticket, which we
forge ahead of time and hand to idevicerestore with -T, the Cryptex1 ticket is
requested by the device in the middle of the restore: restored asks the host for
FirmwareUpdaterData with MessageArgUpdaterName = "Cryptex1", and idevicerestore
turns that into a TSS request to gs.apple.com. Apple won't sign an unsigned
build, so the restore dies there (ChefKiss issue #305).

This is that signing server, for the emulator only. idevicerestore is pointed at
it with --server http://127.0.0.1:<port>; it disables TLS verification for a
custom server, so plain HTTP is fine. We answer the Cryptex1 request with a
forged IM4M whose component digests come straight from the build manifest, so
they match the cryptex images ASR writes, and whose identity (chip, class,
version) matches the build. The signature is inherited from a template ticket
and is invalid, exactly like our forged AP ticket -- the emulator's kernel
patches (`allow unsigned firmware in img4_firmware_evaluate`) don't check it.

    netlab/tssd.py --manifest InfernoData/Restore/BuildManifest.plist \
                   --template cryptex_template.im4m --port 8888
    idevicerestore ... --server http://127.0.0.1:8888

The template is any device's Cryptex1 IM4M, used only for its ASN.1 shape and
its (unchecked) signature blob; every identifying field is overwritten. A Mac's
own lives under /System/Volumes/Preboot/<UUID>/cryptex1/current/apticket.*.im4m.
"""

import argparse
import plistlib
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pyasn1.codec.der.decoder import decode
from pyasn1.codec.der.encoder import encode
from pyasn1.type.char import IA5String
from pyasn1.type.namedtype import NamedType, NamedTypes
from pyasn1.type.tag import Tag, tagClassPrivate, tagFormatConstructed, tagFormatSimple
from pyasn1.type.univ import Integer, OctetString, Sequence, SequenceOf, Set, SetOf
from pyasn1_modules import rfc5280

# Cryptex component four-character code -> build manifest key. The digests the
# device wants signed are exactly these components' Digest fields.
FOURCC_TO_MANIFEST = {
    "caos": "Cryptex1,AppOS",
    "casy": "Cryptex1,AppVolume",
    "trca": "Cryptex1,AppTrustCache",
    "csos": "Cryptex1,SystemOS",
    "cssy": "Cryptex1,SystemVolume",
    "trcs": "Cryptex1,SystemTrustCache",
    "cros": "Cryptex1,RosettaOS",
    "crsy": "Cryptex1,RosettaVolume",
    "trcr": "Cryptex1,RosettaTrustCache",
}


class MANB(Sequence):
    componentType = NamedTypes(
        NamedType("type", IA5String()),
        NamedType("payload", Set()),
    )
    tagSet = Sequence.tagSet.tagExplicitly(
        Tag(tagClassPrivate, tagFormatConstructed, 0x4D414E42)
    )


class IM4M(Sequence):
    componentType = NamedTypes(
        NamedType("type", IA5String()),
        NamedType("ver", Integer()),
        NamedType("manb", SetOf(MANB())),
        NamedType("unk", OctetString()),
        NamedType("cert", SequenceOf(rfc5280.Certificate())),
    )


def _tag(name):
    return Tag(tagClassPrivate, tagFormatSimple, int.from_bytes(name.encode(), "big"))


def _prop(name, value):
    """A MANP property or a component payload entry: private-tagged SEQ of
    (IA5String name, value)."""
    seq = Sequence().subtype(explicitTag=_tag(name))
    seq.setComponentByPosition(0, IA5String(name))
    seq.setComponentByPosition(1, value)
    return seq


def _component(fourcc, digest):
    """A cryptex component: (name, Set{ DGST -> digest })."""
    inner = Set()
    inner.setComponentByPosition(0, _prop("DGST", OctetString(digest)))
    return _prop(fourcc, inner)


def _as_int(value):
    if isinstance(value, str):
        return int(value, 0)
    return int(value)


def forge_cryptex_ticket(manifest, model, template_im4m):
    """Build a Cryptex1 IM4M for the erase identity of `model`, reusing the
    template's MANP scaffold, signature and certificate chain."""
    identity = None
    for candidate in manifest["BuildIdentities"]:
        info = candidate["Info"]
        if info.get("DeviceClass", "").lower() == model and info.get("RestoreBehavior") == "Erase":
            identity = candidate
            break
    if identity is None:
        raise SystemExit(f"{model} erase identity not in the build manifest")
    comps = identity["Manifest"]

    ticket = decode(template_im4m, asn1Spec=IM4M())[0]
    payload = ticket["manb"][0]["payload"]

    # Rebuild the payload: the template's MANP (patched to this build's cryptex
    # identity) followed by this build's cryptex components.
    manp_src = None
    for i in range(len(payload)):
        if str(payload[i][0]) == "MANP":
            manp_src = payload[i][1]
            break
    if manp_src is None:
        raise SystemExit("template ticket has no MANP")

    # The cryptex identity lives in the build identity's Cryptex1,* tags.
    def tag(name, default=None):
        return identity.get(name, default)

    identity_overrides = {
        "fchp": _as_int(tag("Cryptex1,ChipID", 0xFF10)),
        "clas": _as_int(tag("Cryptex1,ProductClass", 0xF1)),
        "type": _as_int(tag("Cryptex1,Type", 1)),
        "styp": _as_int(tag("Cryptex1,SubType", 1)),
    }
    version = tag("Cryptex1,Version")
    manp = Set()
    for j in range(len(manp_src)):
        name = str(manp_src[j][0])
        value = manp_src[j][1]
        if name in identity_overrides:
            value = Integer(identity_overrides[name])
        elif name in ("pave", "vnum") and version is not None:
            value = OctetString(version.encode())
        manp.setComponentByPosition(j, _prop(name, value))

    new_payload = Set()
    new_payload.setComponentByPosition(0, _prop("MANP", manp))
    n = 1
    present = []
    for fourcc, key in FOURCC_TO_MANIFEST.items():
        entry = comps.get(key)
        if entry is None or "Digest" not in entry:
            continue
        new_payload.setComponentByPosition(n, _component(fourcc, entry["Digest"]))
        present.append(fourcc)
        n += 1
    ticket["manb"][0]["payload"] = new_payload
    return encode(ticket), present


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            request = plistlib.loads(body)
        except Exception as error:
            print(f"tssd: could not parse request: {error}", flush=True)
            self._reply_failure()
            return

        wants_cryptex = "@Cryptex1,Ticket" in request or any(
            str(k).startswith("Cryptex1") for k in request
        )
        if not wants_cryptex:
            keys = ", ".join(sorted(str(k) for k in request))
            print(f"tssd: non-Cryptex1 request ({keys}); refusing", flush=True)
            self._reply_failure()
            return

        nonce = request.get("Cryptex1,Nonce")
        print(
            f"tssd: Cryptex1 request, nonce={nonce.hex() if isinstance(nonce, bytes) else nonce}",
            flush=True,
        )
        try:
            im4m, present = forge_cryptex_ticket(self.server.manifest, self.server.model, self.server.template)
        except Exception as error:
            print(f"tssd: forge failed: {error}", flush=True)
            self._reply_failure()
            return
        print(f"tssd: signed Cryptex1 ticket, {len(im4m)} bytes, components {present}", flush=True)

        response = {"Cryptex1,Ticket": im4m}
        self._reply_success(response)

    def _reply_success(self, response):
        xml = plistlib.dumps(response, fmt=plistlib.FMT_XML)
        body = b"STATUS=0&MESSAGE=SUCCESS&REQUEST_STRING=" + xml
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reply_failure(self):
        body = b"STATUS=94&MESSAGE=NOT SIGNED"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--template", required=True)
    parser.add_argument("--model", default="n104ap")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8888)
    args = parser.parse_args()

    manifest = plistlib.load(open(args.manifest, "rb"))
    template = open(args.template, "rb").read()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.manifest = manifest
    server.model = args.model.lower()
    server.template = template
    print(f"tssd: signing Cryptex1 for {server.model} on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
