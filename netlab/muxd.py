#!/usr/bin/env python3
"""A stand-in for usbmuxd that speaks to Inferno's USB socket directly.

The stock setup needs a companion Linux VM for one reason: the emulated iPhone
exports its USB port through `hw/usb/hcd-tcp.c`, which dials a socket and
expects a USB host on the other end, and usbmuxd only knows how to be a host
over libusb. This script is that host. It enumerates the device over the
socket, finds the usbmux interface, runs the mux protocol and its cut-down TCP,
and serves the usbmuxd client protocol on a UNIX socket of its own. Any
libimobiledevice tool pointed at that socket — ideviceinfo, idevicerestore —
then works against the emulator with no second VM:

    netlab/muxd.py --usb /tmp/iusb.sock --socket /tmp/inferno-usbmuxd
    USBMUXD_SOCKET_ADDRESS=UNIX:/tmp/inferno-usbmuxd ideviceinfo -s

Start it before the emulator: the emulator connects once, at start-up. When
the device goes away (a reboot, the end of a restore) it is reported as
detached and the next connection from the emulator is accepted.

The wire formats are usbmuxd's own: `usbmuxd-proto.h` for clients, and
`src/device.c` for the mux header, the 16-byte form from version 2 on, and the
TCP header whose window is shifted down by eight bits.
"""

import argparse
import os
import plistlib
import queue
import socket
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tcpusb import (DESC_CONFIG, DESC_DEVICE, RET_NAK, RET_SUCCESS, USB_TOKEN_IN,
                    USB_TOKEN_OUT, Link, UsbError, listen)

# The usbmux interface, as usbmuxd's usb.h names it.
MUX_CLASS, MUX_SUBCLASS, MUX_PROTOCOL = 255, 254, 2

# usbmuxd's sizes: what one USB read may return, and the largest mux packet sent.
USB_MRU = 16384
# The device's mux receive buffer is 16 KB (usbmuxd's USB_MRU): a larger USB
# write is dropped whole, and the restore dies at NORData. Cap what we send to
# the same 16 KB, header included, as the emulator's own GuestUSB does.
USB_MTU = 16384

MUX_PROTO_VERSION, MUX_PROTO_CONTROL, MUX_PROTO_SETUP, MUX_PROTO_TCP = 0, 1, 2, 6
MUX_MAGIC = 0xFEEDFACE

TH_FIN, TH_SYN, TH_RST, TH_PSH, TH_ACK = 0x01, 0x02, 0x04, 0x08, 0x10
TCP_HEADER = struct.Struct("!HHIIBBHHH")

CLIENT_HEADER = struct.Struct("<IIII")     # length, version, message, tag
MESSAGE_RESULT, MESSAGE_PLIST = 1, 8
RESULT_OK, RESULT_BADCOMMAND, RESULT_BADDEV, RESULT_CONNREFUSED = 0, 1, 2, 3
RESULT_BADVERSION = 6
ENOENT = 2

DEVICE_ID = 1

# A client that stops reading is not allowed to make the queue for the device
# grow without bound; its reader waits instead.
CLIENT_BACKLOG = 4 * 1024 * 1024


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


class MuxConnection:
    """One client's TCP connection to a port on the device."""

    def __init__(self, sport, dport, client):
        self.sport = sport
        self.dport = dport
        self.client = client
        self.tx_seq = 0          # next byte we send
        self.tx_ack = 0          # next byte we expect from the device
        self.tx_acked = 0        # what the device has acknowledged of ours
        self.rx_win = 0          # the device's window, already shifted back up
        self.established = threading.Event()
        self.refused = False
        self.outgoing = bytearray()
        self.lock = threading.Condition()
        self.closing = False     # the client went away: drain, then FIN
        self.close_started = 0.0
        self.closed = False


class Device:
    """The emulated iPhone behind one accepted USB connection."""

    def __init__(self, link):
        self.link = link
        self.version = 0
        self.tx_seq = 0
        self.rx_seq = 0xFFFF
        self.ep_in = self.ep_out = 0
        self.max_packet = 512
        self.product_id = 0
        self.serial = ""
        self.inbound = bytearray()
        self.control = queue.Queue()     # (proto, header, payload) from other threads
        self.connections = {}            # our port → MuxConnection
        self.table_lock = threading.Lock()
        self.next_port = 1
        self.alive = True

    # ---- bring-up ----------------------------------------------------------

    def enumerate(self):
        self.link.reset()
        descriptor = self.link.get_descriptor(DESC_DEVICE, 0, 18)
        self.product_id = int.from_bytes(descriptor[10:12], "little")
        # The device pads its serial string with blanks and control characters.
        # libimobiledevice uses the serial as the UDID, and a plist cannot carry
        # control characters at all, so only the printable part is kept.
        raw_serial = self.link.string(descriptor[16])
        self.serial = "".join(c for c in raw_serial if c.isprintable()).strip() or "INFERNO"
        configs = descriptor[17]
        log(f"device {int.from_bytes(descriptor[8:10], 'little'):04x}:{self.product_id:04x}, "
            f"serial {self.serial}, {configs} configurations")

        # Newest configuration first, as usbmuxd does: the mux interface sits in
        # the last one on a device that offers several.
        for index in reversed(range(configs)):
            head = self.link.get_descriptor(DESC_CONFIG, index, 9)
            total = int.from_bytes(head[2:4], "little")
            raw = self.link.get_descriptor(DESC_CONFIG, index, total)
            found = self._find_mux(raw)
            if found:
                value, self.ep_in, self.ep_out, self.max_packet = found
                self.link.set_configuration(value)
                log(f"configuration {value}: mux IN {self.ep_in:#x}, OUT {self.ep_out:#x}, "
                    f"packet {self.max_packet}")
                return
        raise UsbError(-1, "no usbmux interface on the device")

    @staticmethod
    def _find_mux(raw):
        value = raw[5]
        at, in_mux = 0, False
        ep_in = ep_out = packet = 0
        while at + 2 <= len(raw):
            length, kind = raw[at], raw[at + 1]
            if length == 0:
                break
            if kind == 4:     # interface
                in_mux = (raw[at + 5], raw[at + 6], raw[at + 7]) == (MUX_CLASS, MUX_SUBCLASS, MUX_PROTOCOL)
            elif kind == 5 and in_mux and raw[at + 3] & 0x03 == 2:     # bulk endpoint
                address = raw[at + 2]
                packet = int.from_bytes(raw[at + 4:at + 6], "little")
                if address & 0x80:
                    ep_in = address & 0x0F
                else:
                    ep_out = address & 0x0F
            at += length
        if ep_in and ep_out:
            return value, ep_in, ep_out, packet or 512
        return None

    def handshake(self):
        self._send_packet(MUX_PROTO_VERSION, struct.pack("!III", 2, 0, 0))
        deadline = time.time() + 10
        while time.time() < deadline:
            packet = self._poll_packet()
            if packet is None:
                time.sleep(0.01)
                continue
            proto, body = packet
            if proto == MUX_PROTO_VERSION and len(body) >= 12:
                major, minor, _ = struct.unpack("!III", body[:12])
                self.version = min(major, 2)
                log(f"mux version {major}.{minor}")
                if self.version >= 2:
                    self._send_packet(MUX_PROTO_SETUP, b"\x07")
                return
        raise UsbError(-1, "no answer to the mux VERSION")

    # ---- USB framing -------------------------------------------------------

    def _send_packet(self, proto, header=b"", payload=b""):
        size = (8 if self.version < 2 else 16) + len(header) + len(payload)
        if self.version < 2:
            head = struct.pack("!II", proto, size)
        else:
            if proto == MUX_PROTO_SETUP:
                self.tx_seq, self.rx_seq = 0, 0xFFFF
            head = struct.pack("!IIIHH", proto, size, MUX_MAGIC, self.tx_seq, self.rx_seq)
            self.tx_seq = (self.tx_seq + 1) & 0xFFFF
        data = head + header + payload
        status, _ = self.link.xfer(USB_TOKEN_OUT, self.ep_out, data, retries=2000, delay=0.001)
        if status != RET_SUCCESS:
            raise UsbError(status, "mux OUT")
        # A transfer that ends exactly on a packet boundary needs a zero-length
        # packet, or the device keeps waiting for the rest of it.
        if len(data) % self.max_packet == 0:
            self.link.xfer(USB_TOKEN_OUT, self.ep_out, b"", retries=2000, delay=0.001)

    def _poll_packet(self):
        """Reads whatever the device has and returns one whole packet, if there is one."""
        packet = self._take_packet()
        if packet:
            return packet
        status, chunk = self.link.xfer(USB_TOKEN_IN, self.ep_in, length=USB_MRU, retries=1, delay=0)
        if status == RET_SUCCESS and chunk:
            self.inbound += chunk
            return self._take_packet()
        if status not in (RET_SUCCESS, RET_NAK):
            raise UsbError(status, "mux IN")
        return None

    def _take_packet(self):
        if len(self.inbound) < 8:
            return None
        proto, total = struct.unpack("!II", self.inbound[:8])
        if total < 8:
            raise UsbError(-1, f"bad mux packet length {total}")
        if len(self.inbound) < total:
            return None
        raw = bytes(self.inbound[:total])
        del self.inbound[:total]
        header = 8
        if self.version >= 2 and len(raw) >= 16:
            # Echoing the device's own number back is what keeps a v2 session alive.
            self.rx_seq = struct.unpack("!H", raw[12:14])[0]
            header = 16
        return proto, raw[header:]

    # ---- TCP -----------------------------------------------------------------

    def _send_tcp(self, conn, flags, payload=b""):
        header = TCP_HEADER.pack(conn.sport, conn.dport, conn.tx_seq, conn.tx_ack,
                                 5 << 4, flags, (256 * 1024) >> 8, 0, 0)
        self._send_packet(MUX_PROTO_TCP, header, payload)
        conn.tx_seq = (conn.tx_seq + len(payload) + (1 if flags & TH_SYN else 0)) & 0xFFFFFFFF

    def open(self, dport, client):
        with self.table_lock:
            sport = self.next_port
            self.next_port = self.next_port % 0xFFFE + 1
            conn = MuxConnection(sport, dport, client)
            self.connections[sport] = conn
        self.control.put(("syn", conn))
        return conn

    def _handle_tcp(self, body):
        if len(body) < TCP_HEADER.size:
            return
        sport, dport, seq, ack, _, flags, window, _, _ = TCP_HEADER.unpack(body[:TCP_HEADER.size])
        payload = body[TCP_HEADER.size:]
        with self.table_lock:
            conn = self.connections.get(dport)
        if conn is None:
            if not flags & TH_RST:
                log(f"packet for unknown port {dport}, flags {flags:#x}")
            return
        conn.rx_win = window << 8
        if flags & TH_RST:
            if not conn.established.is_set():
                conn.refused = True
                conn.established.set()
            self._drop(conn, notify_device=False)
            return
        if flags & TH_SYN and flags & TH_ACK:
            conn.tx_ack = (seq + 1) & 0xFFFFFFFF
            conn.tx_acked = ack
            self._send_tcp(conn, TH_ACK)
            conn.established.set()
            return
        if flags & TH_ACK:
            conn.tx_acked = ack
        if payload:
            conn.tx_ack = (seq + len(payload)) & 0xFFFFFFFF
            try:
                conn.client.sendall(payload)
            except OSError:
                self._drop(conn, notify_device=True)
                return
            self._send_tcp(conn, TH_ACK)
        if flags & TH_FIN:
            self._drop(conn, notify_device=True)

    def _drop(self, conn, notify_device):
        if conn.closed:
            return
        conn.closed = True
        if notify_device:
            try:
                self._send_tcp(conn, TH_RST)
            except (UsbError, OSError):
                pass
        with self.table_lock:
            self.connections.pop(conn.sport, None)
        with conn.lock:
            conn.lock.notify_all()
        try:
            conn.client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _flush(self, conn):
        """Sends what the client has queued, as far as the device's window allows."""
        sent = False
        max_payload = USB_MTU - 16 - TCP_HEADER.size
        while True:
            with conn.lock:
                in_flight = (conn.tx_seq - conn.tx_acked) & 0xFFFFFFFF
                room = conn.rx_win - in_flight
                if not conn.outgoing or room <= 0:
                    break
                chunk = bytes(conn.outgoing[:min(len(conn.outgoing), max_payload, room)])
                del conn.outgoing[:len(chunk)]
                conn.lock.notify_all()
            self._send_tcp(conn, TH_ACK, chunk)
            sent = True
        return sent

    # ---- the loop ------------------------------------------------------------

    def run(self):
        """Owns the USB link: every transfer happens on this thread."""
        try:
            while self.alive:
                busy = False
                while True:
                    try:
                        what, conn = self.control.get_nowait()
                    except queue.Empty:
                        break
                    busy = True
                    if what == "syn":
                        self._send_tcp(conn, TH_SYN)
                    elif what == "close":
                        # The client is done. Don't tear the mux connection down
                        # yet: data it queued may still be waiting for the
                        # device's window, and an RST here drops that tail. A
                        # 4.9 GB ASR image failed at ~96% for exactly this. Mark
                        # it closing and let the flush loop drain and FIN it.
                        if not conn.closing and not conn.closed:
                            conn.closing = True
                            conn.close_started = time.monotonic()
                with self.table_lock:
                    open_now = list(self.connections.values())
                for conn in open_now:
                    if conn.closed:
                        continue
                    if conn.established.is_set():
                        busy |= self._flush(conn)
                    if conn.closing:
                        with conn.lock:
                            drained = not conn.outgoing and conn.tx_seq == conn.tx_acked
                        if drained:
                            # All queued bytes are sent and acknowledged; a clean
                            # FIN lets the device's reader see a proper EOF.
                            self._send_tcp(conn, TH_FIN)
                            self._drop(conn, notify_device=False)
                        elif time.monotonic() - conn.close_started > 60:
                            log(f"port {conn.dport}: close drain timed out, "
                                f"{len(conn.outgoing)} queued, seq {conn.tx_seq} acked {conn.tx_acked}")
                            self._drop(conn, notify_device=True)
                        else:
                            busy = True
                packet = self._poll_packet()
                while packet is not None:
                    busy = True
                    proto, body = packet
                    if proto == MUX_PROTO_TCP:
                        self._handle_tcp(body)
                    elif proto != MUX_PROTO_VERSION:
                        log(f"mux packet proto {proto}, {len(body)} bytes")
                    packet = self._take_packet()
                if not busy:
                    time.sleep(0.001)
        except (UsbError, ConnectionError, OSError) as error:
            log(f"device gone: {error}")
        finally:
            self.alive = False
            with self.table_lock:
                leftovers = list(self.connections.values())
            for conn in leftovers:
                if not conn.established.is_set():
                    conn.refused = True
                    conn.established.set()
                self._drop(conn, notify_device=False)


class Muxd:
    def __init__(self, usb_path, socket_path, pair_dir):
        self.usb_path = usb_path
        self.socket_path = socket_path
        self.pair_dir = pair_dir
        self.device = None
        self.listeners = set()
        self.listeners_lock = threading.Lock()
        os.makedirs(pair_dir, exist_ok=True)

    # ---- the device side -----------------------------------------------------

    def serve_usb(self):
        server = listen(self.usb_path)
        log(f"waiting for the emulator on {self.usb_path}")
        while True:
            conn, _ = server.accept()
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
            log("emulator connected")
            # The emulator dials in as soon as it starts, long before the guest's
            # USB stack answers, and it does not dial again. So bring-up is
            # retried on this connection until the device is there.
            device = None
            while device is None:
                time.sleep(3.0)
                candidate = Device(Link(conn))
                try:
                    candidate.enumerate()
                    candidate.handshake()
                    device = candidate
                except UsbError as error:
                    log(f"device not ready: {error}")
                except (ConnectionError, OSError) as error:
                    log(f"emulator went away during bring-up: {error}")
                    break
            if device is None:
                conn.close()
                continue
            self.device = device
            self._broadcast(self._attached())
            device.run()
            self.device = None
            self._broadcast({"MessageType": "Detached", "DeviceID": DEVICE_ID})
            conn.close()

    def _attached(self):
        device = self.device
        return {
            "MessageType": "Attached",
            "DeviceID": DEVICE_ID,
            "Properties": {
                "ConnectionSpeed": 480000000,
                "ConnectionType": "USB",
                "DeviceID": DEVICE_ID,
                "LocationID": 0,
                "ProductID": device.product_id,
                "SerialNumber": device.serial,
            },
        }

    def _broadcast(self, message):
        with self.listeners_lock:
            targets = list(self.listeners)
        for client in targets:
            try:
                self._send_plist(client, 0, message)
            except OSError:
                with self.listeners_lock:
                    self.listeners.discard(client)

    # ---- the client side -----------------------------------------------------

    def serve_clients(self):
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        os.chmod(self.socket_path, 0o666)
        server.listen(16)
        log(f"usbmuxd clients on {self.socket_path}")
        while True:
            client, _ = server.accept()
            threading.Thread(target=self._client, args=(client,), daemon=True).start()

    @staticmethod
    def _recv(sock, size):
        data = b""
        while len(data) < size:
            chunk = sock.recv(size - len(data))
            if not chunk:
                raise ConnectionError("client closed")
            data += chunk
        return data

    @staticmethod
    def _send_plist(sock, tag, message):
        body = plistlib.dumps(message, fmt=plistlib.FMT_XML)
        sock.sendall(CLIENT_HEADER.pack(CLIENT_HEADER.size + len(body), 1, MESSAGE_PLIST, tag) + body)

    def _result(self, sock, tag, number):
        self._send_plist(sock, tag, {"MessageType": "Result", "Number": number})

    def _client(self, sock):
        try:
            while True:
                length, version, message, tag = CLIENT_HEADER.unpack(self._recv(sock, CLIENT_HEADER.size))
                body = self._recv(sock, length - CLIENT_HEADER.size)
                if message != MESSAGE_PLIST:
                    self._result(sock, tag, RESULT_BADVERSION)
                    continue
                request = plistlib.loads(body)
                if self._handle(sock, tag, request) == "raw":
                    return
        except (ConnectionError, OSError, plistlib.InvalidFileException):
            pass
        finally:
            with self.listeners_lock:
                listening = sock in self.listeners
            if not listening:
                sock.close()

    def _handle(self, sock, tag, request):
        kind = request.get("MessageType")
        if kind == "Listen":
            self._result(sock, tag, RESULT_OK)
            with self.listeners_lock:
                self.listeners.add(sock)
            if self.device:
                self._send_plist(sock, 0, self._attached())
            # The socket now only carries events; hold the thread until it closes.
            while sock.recv(1):
                pass
            with self.listeners_lock:
                self.listeners.discard(sock)
            sock.close()
            return "raw"
        if kind == "ListDevices":
            devices = [self._attached()] if self.device else []
            self._send_plist(sock, tag, {"DeviceList": devices})
        elif kind == "ListListeners":
            self._send_plist(sock, tag, {"ListenerList": []})
        elif kind == "ReadBUID":
            self._send_plist(sock, tag, {"BUID": "00000000-0000-0000-0000-00000000C0DE"})
        elif kind == "ReadPairRecord":
            path = os.path.join(self.pair_dir, f"{request.get('PairRecordID')}.plist")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    self._send_plist(sock, tag, {"PairRecordData": f.read()})
            else:
                self._result(sock, tag, ENOENT)
        elif kind == "SavePairRecord":
            path = os.path.join(self.pair_dir, f"{request.get('PairRecordID')}.plist")
            with open(path, "wb") as f:
                f.write(request.get("PairRecordData", b""))
            self._result(sock, tag, RESULT_OK)
        elif kind == "DeletePairRecord":
            path = os.path.join(self.pair_dir, f"{request.get('PairRecordID')}.plist")
            if os.path.exists(path):
                os.unlink(path)
            self._result(sock, tag, RESULT_OK)
        elif kind == "Connect":
            return self._connect(sock, tag, request)
        else:
            log(f"unhandled client request {kind}")
            self._result(sock, tag, RESULT_BADCOMMAND)
        return None

    def _connect(self, sock, tag, request):
        device = self.device
        if device is None or request.get("DeviceID") != DEVICE_ID:
            self._result(sock, tag, RESULT_BADDEV)
            return None
        # The client sends the port in network byte order inside a plain integer.
        raw = int(request.get("PortNumber", 0)) & 0xFFFF
        port = ((raw >> 8) & 0xFF) | ((raw & 0xFF) << 8)
        conn = device.open(port, sock)
        if not conn.established.wait(15) or conn.refused:
            log(f"connect to port {port} refused")
            device.control.put(("close", conn))
            self._result(sock, tag, RESULT_CONNREFUSED)
            return None
        log(f"connected to port {port} (local {conn.sport})")
        self._result(sock, tag, RESULT_OK)

        # From here the socket is a byte pipe to the device's port.
        try:
            while not conn.closed:
                data = sock.recv(65536)
                if not data:
                    break
                with conn.lock:
                    while len(conn.outgoing) > CLIENT_BACKLOG and not conn.closed:
                        conn.lock.wait(1)
                    conn.outgoing += data
        except OSError:
            pass
        if not conn.closed:
            device.control.put(("close", conn))
        log(f"port {port} closed")
        return "raw"


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--usb", default="/tmp/iusb.sock", help="the socket the emulator dials (usb-conn-addr)")
    parser.add_argument("--socket", default="/tmp/inferno-usbmuxd", help="where usbmuxd clients connect")
    parser.add_argument("--pairs", default="/tmp/inferno-usbmuxd-pairs", help="where pair records are kept")
    args = parser.parse_args()
    muxd = Muxd(args.usb, args.socket, args.pairs)
    threading.Thread(target=muxd.serve_clients, daemon=True).start()
    try:
        muxd.serve_usb()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
