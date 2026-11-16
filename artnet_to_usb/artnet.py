"""Art-Net receiver: listens for ArtDmx packets on a chosen network interface."""

import ipaddress
import select
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
        self._stop_event = threading.Event()
        self._filter_net = None
        self._seen = set()
        self._local_ips = {i.ip for i in list_interfaces()}
        self.sock = self._open_socket()
        self.socks = [self.sock]
        self._tx = None  # lazily created, unbound socket for broadcasts off a bound interface
        self.socks_to_close = []
        if self.sock.getsockname()[0] != "127.0.0.1":
            # Consoles on this machine also bind *:6454, and macOS/BSD hand unicast to the
            # first wildcard socket bound, so if the console started first it swallows
            # everything sent to 127.0.0.1. A socket bound to 127.0.0.1 itself is more
            # specific and always wins. Broadcasts still arrive on the wildcard socket only.
            # (Not done for LAN IPs: that would also steal unicast meant for the console,
            # such as other nodes' ArtPollReplies.)
            try:
                self.socks.append(_udp_socket(("127.0.0.1", ARTNET_PORT)))
            except OSError as e:
                self.log(f"Could not bind 127.0.0.1:{ARTNET_PORT} ({e}); local unicast may be missed")

    def _open_socket(self):
        sock = _udp_socket(None)
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

    def _accept(self, addr):
        if self._filter_net is None or addr[0] in self._local_ips or addr[0].startswith("127."):
            return True  # senders on this machine are always accepted
        return ipaddress.IPv4Address(addr[0]) in self._filter_net

    def stop(self):
        self._stop_event.set()

    def run(self):
        where = self.interface.label if self.interface else "all interfaces"
        self.log(f"Listening for Art-Net on {where}, UDP {ARTNET_PORT}")
        try:
            self._send_poll_replies()  # unsolicited announcement
            while not self._stop_event.is_set():
                try:
                    ready, _, _ = select.select(self.socks, [], [], 0.5)
                    received = [s.recvfrom(1024) for s in ready]
                except OSError as e:
                    if not self._stop_event.is_set():
                        self.log(f"Socket error: {e}")
                    break
                for packet, addr in received:
                    if not self._accept(addr):
                        self._log_once(("filtered", addr[0]),
                                       f"Ignoring Art-Net from {addr[0]} (not on {self._filter_net})")
                        continue
                    self._handle_packet(packet, addr)
        finally:
            for s in self.socks + self.socks_to_close:
                s.close()
            self.log("Art-Net receiver stopped")

    def _log_once(self, key, msg):
        if key not in self._seen:
            self._seen.add(key)
            self.log(msg)

    def _handle_packet(self, packet, addr):
        if len(packet) < 10 or packet[:8] != ARTNET_ID:
            self._log_once(("junk", addr[0]), f"Non-Art-Net packet from {addr[0]}:{addr[1]} ({len(packet)} bytes)")
            return
        opcode = struct.unpack_from("<H", packet, 8)[0]
        if opcode == OP_DMX and len(packet) >= 18:
            sub_uni, net, length = struct.unpack_from(">BBH", packet, 14)
            universe = ((net & 0x7F) << 8) | sub_uni
            data = packet[18 : 18 + min(length, 512)]
            self._log_once(("dmx", addr[0], universe),
                           f"ArtDmx from {addr[0]}:{addr[1]} → universe {universe} ({format_universe(universe)}), {len(data)} ch")
            self.store.update(universe, data, addr[0])
        elif opcode == OP_POLL:
            self._log_once(("poll", addr[0]), f"ArtPoll from {addr[0]}:{addr[1]}, replying")
            self._send_poll_replies(addr)
        elif opcode == OP_POLL_REPLY:
            if addr[0] not in self._local_ips:  # skip our own echo
                name = packet[26:44].split(b"\0", 1)[0].decode("ascii", "replace")
                self._log_once(("node", addr[0]), f"Saw Art-Net node {name!r} at {addr[0]}")
        else:
            self._log_once(("op", addr[0], opcode), f"Art-Net opcode 0x{opcode:04x} from {addr[0]}:{addr[1]} (ignored)")

    def _reply_interfaces(self, addr):
        """Interfaces to announce on: the chosen one, else the one facing the poller."""
        if self.interface is not None:
            return [self.interface]
        ifaces = list_interfaces()
        if addr is not None:
            remote = ipaddress.IPv4Address(addr[0])
            match = [i for i in ifaces if remote in i.network]
            if match:
                return match[:1]
            ip = _local_ip_for(addr[0])
            return [i for i in ifaces if i.ip == ip][:1] or [Interface("", ip, "255.255.255.255")]
        return [i for i in ifaces if not i.network.is_loopback]

    def _send_poll_replies(self, addr=None):
        """Send ArtPollReply. addr is the poller, or None for an unsolicited announcement.

        Replies go to the directed broadcast address on port 6454, as the Art-Net spec
        requires. Unicasting back to the poller fails when the controller runs on the same
        host: both sockets share port 6454 and the OS delivers unicast to only one of them,
        often our own.
        """
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
        local_poller = addr is not None and (addr[0] in self._local_ips or addr[0].startswith("127."))
        for iface in self._reply_interfaces(addr):
            sock = self.sock
            if iface.network.is_loopback:
                # Loopback has no broadcast, and unicast to 127.0.0.1:6454 would land in our
                # own 127.0.0.1 socket. A console on this machine also hears LAN broadcasts,
                # so broadcast there (via an interface-neutral socket) and unicast only to
                # pollers listening on a port of their own.
                dests = [addr] if addr is not None and addr[1] != ARTNET_PORT else []
                if local_poller and addr[1] == ARTNET_PORT:
                    dests += [(str(i.network.broadcast_address), ARTNET_PORT)
                              for i in list_interfaces() if not i.network.is_loopback]
                    sock = self._tx_socket()
            else:
                dests = [(str(iface.network.broadcast_address), ARTNET_PORT)]
                if addr is not None and addr[1] != ARTNET_PORT:
                    dests.append(addr)  # poller listens on its own port as well
            mac = _mac_for(iface.name)
            for bind_index, (netsub, unis) in enumerate(chunks, start=1):
                pkt = _poll_reply(iface.ip, netsub, unis, bind_index, mac)
                for dest in dests:
                    try:
                        sock.sendto(pkt, dest)
                    except OSError as e:
                        self._log_once(("replyerr", dest[0], str(e)), f"ArtPollReply to {dest[0]} failed: {e}")

    def _tx_socket(self):
        if self._tx is None:
            self._tx = _udp_socket(None)
            self.socks_to_close.append(self._tx)
        return self._tx

def _udp_socket(bind_addr):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if bind_addr is not None:
        sock.bind(bind_addr)
    return sock


def _mac_for(ifname):
    for addr in psutil.net_if_addrs().get(ifname, []):
        if addr.family == psutil.AF_LINK and addr.address:
            try:
                mac = bytes.fromhex(addr.address.replace(":", "").replace("-", ""))
            except ValueError:
                continue
            if len(mac) == 6:
                return mac
    return bytes(6)


def _local_ip_for(remote_ip):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((remote_ip, ARTNET_PORT))
            return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"


def _poll_reply(ip, netsub, unis, bind_index, mac=bytes(6)):
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
    pkt += mac  # MAC
    pkt += socket.inet_aton(ip)  # BindIp
    pkt += bytes([bind_index, 0x08])  # BindIndex, Status2 (15-bit port address)
    pkt += bytes(4)  # GoodOutputB
    pkt += bytes([0])  # Status3
    pkt += bytes(6)  # DefaultRespUID
    pkt += bytes(2)  # User
    pkt += struct.pack(">H", 44)  # RefreshRate
    pkt += bytes(239 - len(pkt))  # filler
    return bytes(pkt)
