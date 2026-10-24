"""Art-Net receiver: listens for ArtDmx packets on a chosen network interface."""

import ipaddress
import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass

import psutil

ARTNET_PORT = 6454
ARTNET_ID = b"Art-Net\x00"
OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100
OP_DMX = 0x5000

ANY_INTERFACE = "Any (all interfaces)"


@dataclass
class Interface:
    name: str
    ip: str
    netmask: str

    @property
    def label(self):
        return f"{self.name} ({self.ip})"

    @property
    def network(self):
        return ipaddress.IPv4Network(f"{self.ip}/{self.netmask}", strict=False)


def list_interfaces():
    """Return IPv4 interfaces that are up."""
    stats = psutil.net_if_stats()
    result = []
    for name, addrs in psutil.net_if_addrs().items():
        if name in stats and not stats[name].isup:
            continue
        for addr in addrs:
            if addr.family == socket.AF_INET and addr.netmask:
                result.append(Interface(name, addr.address, addr.netmask))
    return result


def format_universe(port_address):
    """Port address (0-32767) as Net:Sub:Universe."""
    return f"{port_address >> 8}:{(port_address >> 4) & 0xF}:{port_address & 0xF}"


class UniverseStore:
    """Thread-safe store of the most recent DMX frame for each universe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frames = {}
        self._stats = {}  # universe -> [packet_count, last_time, source_ip]

    def update(self, universe, data, source):
        with self._lock:
            frame = self._frames.get(universe)
            if frame is None:
                frame = self._frames[universe] = bytearray(512)
            frame[: len(data)] = data
            st = self._stats.setdefault(universe, [0, 0.0, ""])
            st[0] += 1
            st[1] = time.monotonic()
            st[2] = source

    def get(self, universe):
        with self._lock:
            frame = self._frames.get(universe)
            return bytes(frame) if frame is not None else None

    def stats(self):
        with self._lock:
            return {u: tuple(s) for u, s in self._stats.items()}

    def clear(self):
        with self._lock:
            self._frames.clear()
            self._stats.clear()


class ArtNetReceiver(threading.Thread):
    """Receives ArtDmx on one interface (or all) and writes frames into a UniverseStore.

    Also answers ArtPoll so lighting consoles can discover this node.
    """

    def __init__(self, store, interface=None, universes_fn=lambda: [], log=print):
        super().__init__(daemon=True, name="artnet-rx")
        self.store = store
        self.interface = interface  # Interface or None for all
        self.universes_fn = universes_fn
        self.log = log
        self._stop = threading.Event()
        self._filter_net = None
        self.sock = self._open_socket()

    def _open_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.5)

        bind_ip = ""
        iface = self.interface
        if iface is not None:
            # Only accept senders on the interface's subnet. The OS-level binding
            # below is not enough on its own: macOS IP_BOUND_IF scopes outgoing
            # traffic but still delivers packets that arrived on other interfaces.
            self._filter_net = iface.network
            if sys.platform == "darwin":
                ip_bound_if = getattr(socket, "IP_BOUND_IF", 25)
                sock.setsockopt(socket.IPPROTO_IP, ip_bound_if, socket.if_nametoindex(iface.name))
            elif sys.platform.startswith("linux"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, 25, iface.name.encode() + b"\0")  # SO_BINDTODEVICE
                except OSError:
                    pass  # needs CAP_NET_RAW; the subnet filter still applies
            else:
                # Windows delivers broadcasts to sockets bound to an interface IP.
                bind_ip = iface.ip
        sock.bind((bind_ip, ARTNET_PORT))
        return sock

    def stop(self):
        self._stop.set()

    def run(self):
        where = self.interface.label if self.interface else "all interfaces"
        self.log(f"Listening for Art-Net on {where}, UDP {ARTNET_PORT}")
        try:
            while not self._stop.is_set():
                try:
                    packet, addr = self.sock.recvfrom(1024)
                except socket.timeout:
                    continue
                except OSError as e:
                    if not self._stop.is_set():
                        self.log(f"Socket error: {e}")
                    break
                if self._filter_net and ipaddress.IPv4Address(addr[0]) not in self._filter_net:
                    continue
                self._handle(packet, addr)
        finally:
            self.sock.close()
            self.log("Art-Net receiver stopped")

    def _handle(self, packet, addr):
        if len(packet) < 10 or packet[:8] != ARTNET_ID:
            return
        opcode = struct.unpack_from("<H", packet, 8)[0]
        if opcode == OP_DMX and len(packet) >= 18:
            sub_uni, net, length = struct.unpack_from(">BBH", packet, 14)
            universe = ((net & 0x7F) << 8) | sub_uni
            data = packet[18 : 18 + min(length, 512)]
            self.store.update(universe, data, addr[0])
        elif opcode == OP_POLL:
            self._send_poll_replies(addr)

    def _send_poll_replies(self, addr):
        universes = sorted(set(self.universes_fn()))
        # One reply per group of up to 4 universes sharing Net/Sub-Net.
        groups = {}
        for u in universes:
            groups.setdefault(u >> 4, []).append(u & 0xF)
        chunks = []
        for netsub, unis in sorted(groups.items()):
            for i in range(0, len(unis), 4):
                chunks.append((netsub, unis[i : i + 4]))
        if not chunks:
            chunks = [(0, [])]
        ip = self.interface.ip if self.interface else _local_ip_for(addr[0])
        for bind_index, (netsub, unis) in enumerate(chunks, start=1):
            try:
                self.sock.sendto(_poll_reply(ip, netsub, unis, bind_index), addr)
            except OSError as e:
                self.log(f"ArtPollReply failed: {e}")


def _local_ip_for(remote_ip):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((remote_ip, ARTNET_PORT))
            return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"


def _poll_reply(ip, netsub, unis, bind_index):
    port_types = bytes([0x80 if i < len(unis) else 0 for i in range(4)])  # 0x80 = DMX output
    good_output = bytes([0x80 if i < len(unis) else 0 for i in range(4)])
    sw_out = bytes(unis + [0] * (4 - len(unis)))
    pkt = bytearray()
    pkt += ARTNET_ID
    pkt += struct.pack("<H", OP_POLL_REPLY)
    pkt += socket.inet_aton(ip)
    pkt += struct.pack("<H", ARTNET_PORT)
    pkt += struct.pack(">H", 1)  # firmware version
    pkt += bytes([(netsub >> 4) & 0x7F, netsub & 0xF])  # NetSwitch, SubSwitch
    pkt += struct.pack(">H", 0x00FF)  # OEM unknown
    pkt += bytes([0, 0xD0])  # UBEA version, Status1 (normal indicators)
    pkt += struct.pack("<H", 0)  # ESTA manufacturer
    pkt += b"ArtNet-to-USB".ljust(18, b"\0")
    pkt += b"ArtNet-to-USB serial bridge".ljust(64, b"\0")
    pkt += b"#0001 [0000] OK".ljust(64, b"\0")
    pkt += struct.pack(">H", len(unis))
    pkt += port_types
    pkt += bytes(4)  # GoodInput
    pkt += good_output
    pkt += bytes(4)  # SwIn
    pkt += sw_out
    pkt += bytes([0, 0, 0])  # AcnPriority, SwMacro, SwRemote
    pkt += bytes(3)  # spare
    pkt += bytes([0x00])  # Style: StNode
    pkt += bytes(6)  # MAC
    pkt += socket.inet_aton(ip)  # BindIp
    pkt += bytes([bind_index, 0x08])  # BindIndex, Status2 (15-bit port address)
    pkt += bytes(4)  # GoodOutputB
    pkt += bytes([0])  # Status3
    pkt += bytes(6)  # DefaultRespUID
    pkt += bytes(2)  # User
    pkt += struct.pack(">H", 44)  # RefreshRate
    pkt += bytes(239 - len(pkt))  # filler
    return bytes(pkt)
