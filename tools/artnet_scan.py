#!/usr/bin/env python3
"""Art-Net node scanner: send ArtPoll, decode every ArtPollReply.

Acts like a lighting console doing node discovery, so you can test any Art-Net
node (including this app) without VDMX, grandMA, etc.

    python tools/artnet_scan.py                    # poll every interface's broadcast
    python tools/artnet_scan.py --target 192.168.178.148
    python tools/artnet_scan.py --bind-6454        # listen on 6454 like real consoles
    python tools/artnet_scan.py --listen 30        # just print replies/polls for 30 s

Standard library only.
"""

import argparse
import ipaddress
import socket
import struct
import subprocess
import sys
import time

PORT = 6454
ID = b"Art-Net\x00"
OPS = {0x2000: "ArtPoll", 0x2100: "ArtPollReply", 0x5000: "ArtDmx", 0x5200: "ArtSync"}
STYLES = {0: "StNode", 1: "StController", 2: "StMedia", 3: "StRoute", 4: "StBackup", 5: "StConfig", 6: "StVisual"}


def broadcast_addresses():
    """Directed broadcast address of each non-loopback IPv4 interface (via ifconfig/ip)."""
    out = set()
    try:
        text = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "inet" and "netmask" in parts:
                ip = parts[1]
                mask = parts[parts.index("netmask") + 1]
                if mask.startswith("0x"):
                    mask = str(ipaddress.IPv4Address(int(mask, 16)))
                net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
                if not net.is_loopback:
                    out.add(str(net.broadcast_address))
    except FileNotFoundError:
        pass
    return sorted(out) or ["255.255.255.255"]


def art_poll():
    # ID, OpCode, ProtVer 14, Flags (0x02 = send ArtPollReply on change), DiagPriority
    return ID + struct.pack("<H", 0x2000) + struct.pack(">H", 14) + bytes([0x02, 0x00])


def cstr(b):
    return b.split(b"\0", 1)[0].decode("ascii", "replace")


def decode_reply(p):
    if len(p) < 207:
        return f"  !! too short: {len(p)} bytes (spec: 239)"
    ip = socket.inet_ntoa(p[10:14])
    port = struct.unpack_from("<H", p, 14)[0]
    net, sub = p[18], p[19]
    num_ports = struct.unpack_from(">H", p, 172)[0]
    port_types, good_out, sw_out = p[174:178], p[182:186], p[190:194]
    lines = [
        f"  IP {ip}  port {port}  len {len(p)}",
        f"  short name : {cstr(p[26:44])!r}",
        f"  long name  : {cstr(p[44:108])!r}",
        f"  report     : {cstr(p[108:172])!r}",
        f"  OEM 0x{struct.unpack_from('>H', p, 20)[0]:04x}  ESTA 0x{struct.unpack_from('<H', p, 24)[0]:04x}"
        f"  Status1 0x{p[23]:02x}  Style {STYLES.get(p[200], p[200])}",
        f"  MAC {p[201:207].hex(':')}",
    ]
    if len(p) >= 213:
        lines.append(f"  BindIp {socket.inet_ntoa(p[207:211])}  BindIndex {p[211]}  Status2 0x{p[212]:02x}")
    lines.append(f"  NumPorts {num_ports}  Net {net}  Sub {sub}")
    for i in range(4):
        if port_types[i]:
            uni = (net << 8) | (sub << 4) | (sw_out[i] & 0xF)
            kind = "out" if port_types[i] & 0x80 else ""
            kind += "+in" if port_types[i] & 0x40 else ""
            lines.append(f"    port {i + 1}: {kind or 'none'} universe {uni} ({net}:{sub}:{sw_out[i] & 0xF})"
                         f"  type 0x{port_types[i]:02x} goodout 0x{good_out[i]:02x}")
    if port != PORT:
        lines.append(f"  !! port field is {port}, expected {PORT}")
    if len(p) != 239:
        lines.append(f"  !! length {len(p)}, spec says 239")
    return "\n".join(lines)


def make_socket(bind_6454):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if bind_6454:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", PORT if bind_6454 else 0))
    s.settimeout(0.2)
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", action="append", help="poll this address (repeatable; default: all broadcasts)")
    ap.add_argument("--bind-6454", action="store_true", help="listen on UDP 6454 like most consoles")
    ap.add_argument("--time", type=float, default=3.0, help="seconds to wait for replies (default 3)")
    ap.add_argument("--listen", type=float, metavar="SECONDS", help="don't poll, just print traffic")
    args = ap.parse_args()

    s = make_socket(args.bind_6454 or args.listen)
    print(f"Listening on UDP {s.getsockname()[1]}")
    if args.listen:
        deadline = time.time() + args.listen
    else:
        targets = args.target or broadcast_addresses()
        for t in targets:
            s.sendto(art_poll(), (t, PORT))
            print(f"ArtPoll -> {t}:{PORT}")
        deadline = time.time() + args.time

    nodes = set()
    while time.time() < deadline:
        try:
            p, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        if p[:8] != ID or len(p) < 10:
            continue
        op = struct.unpack_from("<H", p, 8)[0]
        if op == 0x2100:
            nodes.add((p[10:14], p[211] if len(p) > 211 else 0))
            print(f"\nArtPollReply from {addr[0]}:{addr[1]}")
            print(decode_reply(p))
        elif args.listen:
            print(f"{OPS.get(op, hex(op))} from {addr[0]}:{addr[1]} ({len(p)} bytes)")
    if not args.listen:
        print(f"\n{len(nodes)} node(s)/bind(s) found")


if __name__ == "__main__":
    sys.exit(main())
