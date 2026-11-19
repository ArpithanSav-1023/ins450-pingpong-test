#!/usr/bin/env python3
"""
test_sender.py - send IEC 61162-450 test datagrams to every Table 4 transmission group.

Each datagram is built fresh: running n: line count, current UTC time, correct TAG and
sentence checksums, and the DSCP (IP_TOS) of the group's priority class (IEC 61162-460 Table 1,
plus the project choices for BAM/CAM, NETA and PGN groups).

    python3 test_sender.py --list                         # show groups, addresses, priorities
    python3 test_sender.py --dry-run --count 1            # print what would be sent, send nothing
    python3 test_sender.py --iface 192.168.2.50           # send all groups once per second on that card
    python3 test_sender.py --iface 192.168.2.50 --groups NAVD SATD BAM1 --rate 5 --count 50
    python3 test_sender.py --iface 192.168.2.50 --groups SYSLOG BINRR

Options
  --iface IP        IP address of the network card to send from (Switch A or Switch B side)
  --groups ...      group names (default: all Table 4 groups + SYSLOG); also BINRA, BINRR (Table 5)
  --rate HZ         rounds per second (default 1)
  --count N         number of rounds, 0 = until Ctrl+C (default 0)
  --ttl N           multicast TTL (default 64, IEC 61162-450 default)
  --no-dscp         do not set IP_TOS
  --system-sfi      use the real system SFIs (SI0001, IN0003 ...). Default uses test SFIs
                    (SI9001, IN9003 ...: same talker, 9000 range) so test data cannot be
                    mistaken for real sensors.
  --dry-run         print payloads (hex for binary) without sending
  --pcap FILE       also write every datagram as a full Ethernet frame to FILE (open in
                    Wireshark); with --dry-run nothing is sent, only the file is written
  --src-ip IP       source IP used in the pcap frames (default: --iface or 192.168.2.50)

SAFETY: this sends position, heading, alerts and SRP. Use an isolated test network, or keep
the default test SFIs. Alert texts are prefixed "TEST".
Windows: IP_TOS is ignored unless a Policy-based QoS rule sets DSCP for this program.
"""
import argparse
import datetime
import random
import socket
import struct
import sys
import time
from functools import reduce

# group: (address, port, CoS, basis)
GROUPS = {
    "MISC": ("239.192.0.1", 60001, 2, "Table 1"),
    "TGTD": ("239.192.0.2", 60002, 5, "Table 1"),
    "SATD": ("239.192.0.3", 60003, 5, "Table 1"),
    "NAVD": ("239.192.0.4", 60004, 5, "Table 1"),
    "VDRD": ("239.192.0.5", 60005, 3, "Table 1"),
    "RCOM": ("239.192.0.6", 60006, 4, "Table 1"),
    "TIME": ("239.192.0.7", 60007, 3, "Table 1"),
    "PROP": ("239.192.0.8", 60008, 1, "Table 1"),
    **{f"USR{i}": (f"239.192.0.{8 + i}", 60008 + i, 1, "Table 1") for i in range(1, 9)},
    "BAM1": ("239.192.0.17", 60017, 3, "project"),
    "BAM2": ("239.192.0.18", 60018, 3, "project"),
    "CAM1": ("239.192.0.19", 60019, 3, "project"),
    "CAM2": ("239.192.0.20", 60020, 3, "project"),
    "NETA": ("239.192.0.56", 60056, 7, "network control"),
    **{f"PGP{i}": (f"239.192.0.{56 + i}", 60056 + i, 2, "project") for i in range(1, 5)},
    **{f"PGB{i}": (f"239.192.0.{60 + i}", 60060 + i, 2, "project") for i in range(1, 5)},
    # extras (not in Table 4)
    "SYSLOG": ("239.192.0.254", 514, 7, "Table 6 · network management"),
    "BINRA": ("239.192.0.21", 60021, 2, "Table 5 · simple binary"),
    "BINRR": ("239.192.0.26", 60026, 4, "Table 5 · re-transmittable binary (VDR image)"),
}
TABLE4 = [g for g in GROUPS if g not in ("SYSLOG", "BINRA", "BINRR")]

# real system SFI -> test SFI
TEST_SFI = {}


def sfi(real, use_real):
    if use_real:
        return real
    TEST_SFI[real] = real[:2] + "9" + real[3:]       # SI0001 -> SI9001: same talker, test range
    return TEST_SFI[real]


def xor(s):
    return reduce(lambda a, c: a ^ ord(c), s, 0)


class Builder:
    def __init__(self, use_real_sfi):
        self.real = use_real_sfi
        self.n = {}
        self.block = random.randint(0, 2 ** 32 - 1)

    def line(self, src):
        self.n[src] = self.n.get(src, 0) % 999 + 1          # n: runs 1..999
        return self.n[src]

    def sentence(self, src_real, body, dest=None, with_c=False):
        src = sfi(src_real, self.real)
        tag = f"s:{src}"
        if dest:
            tag += f",d:{sfi(dest, self.real)}"
        tag += f",n:{self.line(src)}"
        if with_c:
            tag += f",c:{int(time.time() * 1000)}"
        start, text = body[0], body[1:]
        return (f"UdPbC\0\\{tag}*{xor(tag):02X}\\{start}{text}*{xor(text):02X}\r\n").encode("ascii")

    # 38-byte IEC 61162-450 binary header (big-endian, Table 9)
    def header(self, token, src, dst, typ, seq, maxseq, dev=1, ch=1):
        return (token.encode() + b"\0" + struct.pack(">HH", 2, 38) + src.encode()[:6].ljust(6, b"\0")
                + dst.encode()[:6].ljust(6, b"\0") + struct.pack(">HIIIBB", typ, self.block, seq, maxseq, dev, ch))


def utc():
    now = datetime.datetime.now(datetime.timezone.utc)
    return now, now.strftime("%H%M%S.") + f"{now.microsecond // 10000:02d}"


def payloads(group, b):
    """Return a list of payloads (bytes) for one round of this group."""
    now, hms = utc()
    s = b.sentence
    if group == "MISC":
        return [s("SI0007", f"$ERRPM,E,1,{80 + random.random() * 10:.1f},12.5,A", with_c=True)]
    if group == "TGTD":
        return [s("SI0006", "!AIVDM,1,1,,A,13u?etPv2;0n:dDPwUM1U1Cb069D,0"),
                s("RA0001", f"$RATTM,01,2.35,045.0,T,12.0,180.0,T,0.5,10.0,N,TGT01,T,,{hms},A")]
    if group == "SATD":
        hdg = 45.0 + random.uniform(-0.5, 0.5)
        return [s("SI0003", f"$HEHDT,{hdg:05.1f},T"), s("SI0003", "$HEROT,2.5,A"),
                s("IN0005", f"$INTHS,{hdg:05.1f},A")]
    if group == "NAVD":
        return [s("SI0001", f"$GPGGA,{hms},1257.9700,N,07746.1100,E,1,09,0.9,12.0,M,-86.0,M,,"),
                s("SI0011", f"$GPGGA,{hms},1257.9702,N,07746.1097,E,1,08,1.0,12.4,M,-86.0,M,,"),
                s("SI0004", "$SDDPT,12.5,0.5,100.0"),
                s("SI0005", "$VDVBW,12.3,0.1,A,12.4,0.2,A,,V,,V"),
                s("IN0003", f"$INGLL,1257.9712,N,07746.1098,E,{hms},A,D")]
    if group == "VDRD":
        return [s("FR0001", f"$FRALR,{hms},001,A,V,TEST fire detector loop 1 zone 3")]
    if group == "RCOM":
        return [s("CD0001", "$CDTXT,01,01,01,TEST DSC watch on CH70")]
    if group == "TIME":
        return [s("ZC0001", f"$ZCZDA,{hms},{now:%d},{now:%m},{now:%Y},00,00")]
    if group == "PROP":
        return [s("PX0001", "$PXYZRD,1,STBY,24.0")]
    if group.startswith("USR"):
        i = int(group[3:])
        name = ["ENGROOM", "BRIDGE", "STEERING", "GALLEY", "CARGO1", "CARGO2", "PUMPRM", "MAST"][i - 1]
        return [s(f"U{i}0001", f"$U{i}XDR,C,{20 + i + random.random():.1f},C,{name}")]
    if group in ("BAM1", "BAM2"):
        src = "IN0002" if group == "BAM1" else "RA0001"
        talker = src[:2]
        aid = "3168" if group == "BAM1" else "3008"
        return [s(src, f"${talker}ALF,1,1,0,{hms},B,W,V,,{aid},1,1,0,TEST alert")]
    if group in ("CAM1", "CAM2"):
        dest = "IN0002" if group == "CAM1" else "RA0001"
        aid = "3168" if group == "CAM1" else "3008"
        return [s("CA0001", f"$CAACN,{hms},,{aid},1,A,C", dest=dest)]
    if group == "NETA":
        src = sfi("IN0002", b.real)
        body = "INSRP,,02AABBCCDD01,192.168.2.50"
        tag = f"s:{src}"
        return [f"UdPbC\0\\{tag}*{xor(tag):02X}\\${body}*{xor(body):02X}\r\n".encode(),
                f"UdPbC\0\\{tag}*{xor(tag):02X}\\$INSRP,,,*{xor('INSRP,,,'):02X}\r\n".encode()]
    if group.startswith(("PGP", "PGB")):
        i = int(group[3:])
        pgn, prio, data = [
            (129025, 2, struct.pack("<ii", int(12.966167 * 1e7), int(77.768500 * 1e7))),        # position rapid
            (127250, 2, struct.pack("<BHhhB", 1, int(0.7889 * 1e4), 0, 0, 0xFD)),                # heading (rad*1e4)
            (128259, 2, struct.pack("<BHHBBB", 1, int(6.33 * 100), int(6.38 * 100), 0, 0xFF, 0xFF)),  # speed m/s
            (128267, 3, struct.pack("<BIhB", 1, 1250, 50, 0xFF)),                                 # depth cm
        ][i - 1]
        src = sfi("NK0001" if group.startswith("PGP") else "NK0002", b.real)
        hdr = b.header("NkPgN", src, "XXXXXX", 1, 1, 1)
        desc = (b"TESTNAME" + struct.pack(">H", 35) + b"\xff" * 8 + struct.pack(">H", 255)
                + struct.pack(">IB", pgn, prio) + b"\0" * 7)                                    # 32 bytes
        return [hdr + desc + data]
    if group == "SYSLOG":
        src = sfi("SI0001", b.real)
        return [f"<131>1 {now:%Y-%m-%dT%H:%M:%S}Z 192.168.2.175 450-{src} - 103 - TEST TAG or sentence format error".encode()]
    if group == "BINRA":
        content = b"TEST simple binary file " + now.isoformat().encode()
        dt = b"text/plain\0"
        desc = struct.pack(">IIHHB", 13 + len(dt), len(content), 0, 60021, len(dt)) + dt + struct.pack(">H", 0)
        b.block = (b.block + 1) % 2 ** 32
        return [b.header("RaUdP", sfi("EI0001", b.real), "XXXXXX", 1, 1, 1, 1, 2) + desc + content]
    if group == "BINRR":
        content = b"TEST re-transmittable block " + now.isoformat().encode()
        dt = b"text/plain\0"
        desc = struct.pack(">IIHHB", 13 + len(dt), len(content), 0, 60026, len(dt)) + dt + struct.pack(">H", 0)
        b.block = (b.block + 1) % 2 ** 32
        return [b.header("RrUdP", sfi("RA0001", b.real), sfi("VR0001", b.real), 1, 1, 1) + desc + content]
    raise ValueError(group)


def _csum(b):
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack(f">{len(b) // 2}H", b))
    while s >> 16:
        s = (s & 0xFFFF) + (s >> 16)
    return ~s & 0xFFFF


def frame(src_ip, dst_ip, sport, dport, tos, ttl, payload, ident):
    """Ethernet + IPv4 + UDP frame with correct IP and UDP checksums."""
    d = socket.inet_aton(dst_ip)
    dst_mac = bytes([0x01, 0x00, 0x5E, d[1] & 0x7F, d[2], d[3]])
    src_mac = bytes.fromhex("02aabbccdd01")
    s_ = socket.inet_aton(src_ip)
    ulen = 8 + len(payload)
    pseudo = s_ + d + struct.pack(">BBH", 0, 17, ulen)
    udp = struct.pack(">HHHH", sport, dport, ulen, 0) + payload
    uc = _csum(pseudo + udp) or 0xFFFF
    udp = udp[:6] + struct.pack(">H", uc) + udp[8:]
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, tos, 20 + ulen, ident, 0x4000, ttl, 17, 0, s_, d)
    ip = ip[:10] + struct.pack(">H", _csum(ip)) + ip[12:]
    return dst_mac + src_mac + b"\x08\x00" + ip + udp


def show(group, p):
    if p.startswith(b"UdPbC") or p.startswith(b"<"):
        return p.decode("ascii").replace("\0", "\\0").rstrip()
    return p.hex(" ")


def main():
    ap = argparse.ArgumentParser(description="IEC 61162-450 test sender")
    ap.add_argument("--iface")
    ap.add_argument("--groups", nargs="+", default=TABLE4 + ["SYSLOG"])
    ap.add_argument("--rate", type=float, default=1.0)
    ap.add_argument("--count", type=int, default=0)
    ap.add_argument("--ttl", type=int, default=64)
    ap.add_argument("--no-dscp", action="store_true")
    ap.add_argument("--system-sfi", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--pcap")
    ap.add_argument("--src-ip")
    a = ap.parse_args()

    if a.list:
        print(f"{'Group':<7} {'Address':<15} {'Port':>5}  CoS  DSCP  IP_TOS  Basis")
        for g, (addr, port, cos, basis) in GROUPS.items():
            print(f"{g:<7} {addr:<15} {port:>5}   {cos}   CS{cos:<2}  0x{cos << 5:02X}    {basis}")
        return
    groups = []
    for g in a.groups:
        g = g.upper()
        if g == "ALL":
            groups += TABLE4
        elif g in GROUPS:
            groups.append(g)
        else:
            ap.error(f"unknown group {g}; use --list")
    pc = None
    if a.pcap:
        pc = open(a.pcap, "wb")
        pc.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))   # pcap, Ethernet
        src_ip = a.src_ip or a.iface or "192.168.2.50"
    ident = 0
    if not a.dry_run and not a.iface:
        ap.error("give --iface (the IP of the card to send from), or use --dry-run")

    socks = {}
    if not a.dry_run:
        for cos in sorted({GROUPS[g][2] for g in groups}):
            sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sk.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, a.ttl)
            sk.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(a.iface))
            sk.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            if not a.no_dscp:
                sk.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, cos << 5)   # DSCP = CS<cos>
            sk.bind((a.iface, 0))
            socks[cos] = sk

    b = Builder(a.system_sfi)
    sent, rnd = 0, 0
    print(("DRY RUN - nothing is sent. " if a.dry_run else f"Sending from {a.iface}, TTL {a.ttl}. ")
          + ("System SFIs." if a.system_sfi else "Test SFIs (9000 range, e.g. SI9001).") + " Ctrl+C to stop.\n")
    try:
        while a.count == 0 or rnd < a.count:
            rnd += 1
            t0 = time.monotonic()
            for g in groups:
                addr, port, cos, _ = GROUPS[g]
                for p in payloads(g, b):
                    if pc:
                        ident = (ident + 1) & 0xFFFF
                        f = frame(src_ip, addr, 50000 + cos, port, 0 if a.no_dscp else cos << 5, a.ttl, p, ident)
                        ts = time.time()
                        pc.write(struct.pack("<IIII", int(ts), int(ts % 1 * 1e6), len(f), len(f)) + f)
                    if a.dry_run:
                        print(f"{g:<6} {addr}:{port}  CS{cos}  {show(g, p)}")
                    else:
                        socks[cos].sendto(p, (addr, port))
                    sent += 1
            if not a.dry_run:
                print(f"\rround {rnd}: {sent} datagrams sent to {len(groups)} groups", end="", flush=True)
            time.sleep(max(0.0, 1.0 / a.rate - (time.monotonic() - t0)))
    except KeyboardInterrupt:
        pass
    if pc:
        pc.close()
        print(f"\nWrote {sent} frames to {a.pcap}")
    print(f"\nDone: {sent} datagrams.")
    if not a.system_sfi and TEST_SFI:
        print("Test SFIs used: " + ", ".join(f"{v}={k}" for k, v in TEST_SFI.items()))


if __name__ == "__main__":
    sys.exit(main())
