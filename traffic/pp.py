"""Ping-pong endpoint for the IEC 61162-450 multicast test.

One copy runs behind GE1 (role A, namespace tx) and one behind GE9 (role B, namespace rx).

  A --mix round (every group, from the 450 sample mix)--> B      (B counts it, then ECHOES one datagram per datagram received)
  A <--echo on the same group-------------------------------B      (A counts the replies; A never echoes: no storm)
  A <--> B   paced UDP ONF dump (unicast :20100, real S-57 chart bytes as the payload) in BOTH directions to reach the target Mbit/s
             (each sensor may send at most 13 sentences/s, so the 450 mix stays small and the ONF dump carries the bulk of the load)

Every datagram carries an in-band counter so loss is exact, not estimated:
  NMEA 0183 (UdPbC)      tag-block n:<1..999> per source SFI
  binary (NkPgN/RaUdP/RrUdP)  header block-id field = running counter per group
  syslog                 RFC 5424 PROCID field = running counter
  filler                 8-byte sequence number
  radar (optional)       the official HALO20 spoke stream replayed verbatim (17160-byte datagrams, IP-fragmented, from data/radar_bundles.bin, see
                         tools/extract_radar.py); counter = first spoke's sequence number at payload offset 10 (u16 LE, +32 per datagram, wraps at 4096)
Definitions used in the metrics:
  message = one 450 sample (sentence / binary block / syslog line) from the mix
  packet  = any UDP datagram (mix + filler)         data = payload bytes of those
Stats are written to <run-dir>/stats_<role>.json every second (read by monitor/pp_exporter.py).
Lab only: multicast TTL 1, 239.0.0.0/8 only, kill switch .STOP (scripts/estop.sh), 900 s cap.
"""
import argparse
import base64
import random
import json
import os
import re
import selectors
import socket
import struct
import subprocess
import multiprocessing as mp
import threading
import time
import zlib

from . import common as c
from . import selection
from . import sender450 as s450

FILL_PORT = 20100            # CCRS JSON port from the sheet: the ONF dump (no 450 header) is the load filler
FILL_PAYLOAD = 1400          # UDP payload bytes of one filler packet (wire = +46: Eth+FCS 18, IP 20, UDP 8)
# native stream program (native/ppstream, Rust): moves the bulk streams with recvmmsg/sendmmsg at a few percent CPU. Used when present (PP_NATIVE=path, or
# bin/ppstream, or native/ppstream/target/release/ppstream); PP_NATIVE=0 forces the Python streams.
def native_binary():
    env = os.environ.get("PP_NATIVE")
    if env == "0":
        return None
    for p in ([env] if env else []) + [os.path.join(c.PROJECT_ROOT, "bin", "ppstream"), os.path.join(c.PROJECT_ROOT, "native", "ppstream", "target", "release", "ppstream")]:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


FILL_MAGIC = b"FLL"           # stream packet header: FILL_MAGIC + flow id (1 byte), per-flow sequence number, pool offset
FILL_HDR = struct.Struct(">4sQI")   # magic, seq, pool offset
WIRE_OVERHEAD = 46
MAX_SENTENCES_PER_SENSOR = 13   # one sensor (talker/SFI) may send at most 13 sentences per second
BINARY_TOKENS = (b"NkPgN", b"RaUdP", b"RrUdP")
NM_TAG = re.compile(rb"^UdPbC\x00\\([^*\\]*)\*([0-9A-Fa-f]{2})\\(.*)$", re.S)
SYSLOG_N = re.compile(rb"450-(\S+) (\d+) 103 ")
RADAR_FILE = os.path.join(c.PROJECT_ROOT, "data", "radar_bundles.bin")


def radar_wire_bytes(n):
    """Wire bytes of one radar datagram with n payload bytes: UDP 8, IP header 20 and Eth+FCS 18 on every fragment (1480 B of IP payload each)."""
    return n + 8 + -(-(n + 8) // 1480) * 38


def load_radar(path=RADAR_FILE):
    """-> (datagrams, datagrams per second) from the slice written by tools/extract_radar.py."""
    try:
        with open(path, "rb") as f:
            if f.read(4) != b"RADR":
                raise ValueError("bad magic")
            n, hz = struct.unpack("<Id", f.read(12))
            out = [f.read(struct.unpack("<I", f.read(4))[0]) for _ in range(n)]
    except (OSError, ValueError, struct.error) as ex:
        raise SystemExit(f"radar stream requested but {path} is missing/unreadable ({ex}). Build it: .venv/bin/python tools/extract_radar.py <radar_dump.pcapng>")
    return out, hz


def radar_seq(p):
    """in-band counter of a radar datagram: first spoke sequence / 32 (wraps at 128 datagrams = one antenna revolution)."""
    return (struct.unpack_from("<H", p, 10)[0] // 32) % 128 if len(p) >= 12 else None


def xor(b):
    x = 0
    for ch in b:
        x ^= ch
    return x


class Vary:
    """Rate factor in [lo, 1.0]: redrawn every `step` seconds (smoothed random, now and then a peak at 1.0), so the offered load wobbles below its MAXIMUM.
    lo >= 1 means constant at the maximum. Own random generator (the streams run in forked processes and must not wobble in lockstep)."""

    def __init__(self, lo=1.0, step=2.0):
        self.lo, self.step, self.f, self.t, self.rng = float(lo), float(step), 1.0, -1e9, random.Random(int.from_bytes(os.urandom(8), "big"))

    def factor(self, now):
        if self.lo >= 1.0:
            return 1.0
        if now - self.t >= self.step:
            self.t = now
            r = self.rng.random()
            self.f = 1.0 if r < 0.15 else self.lo if r < 0.25 else self.rng.uniform(self.lo, 1.0)      # now and then a peak at the maximum or a dip to the minimum
        return self.f


class Stamper:
    """Puts a running counter into every datagram (see module doc)."""

    def __init__(self):
        self.n = {}

    def _next(self, key, mod):
        v = self.n.get(key, 0) % mod + 1
        self.n[key] = v
        return v

    def stamp(self, group, p):
        if p.startswith(b"UdPbC"):
            m = NM_TAG.match(p)
            if not m:
                return p
            params = [x for x in m.group(1).decode().split(",") if not x.startswith("n:")]
            sfi = next((x[2:] for x in params if x.startswith("s:")), "?")
            v = self._next((group, sfi), 999)
            params.insert(len([x for x in params if x[:2] in ("s:", "d:")]), f"n:{v}")
            tag = ",".join(params).encode()
            return b"UdPbC\x00\\" + tag + b"*%02X\\" % xor(tag) + m.group(3)
        if p[:5] in BINARY_TOKENS:
            return p[:24] + struct.pack(">I", self._next(group, 2 ** 32 - 1)) + p[28:]
        if p.startswith(b"<"):
            return p.replace(b" - 103 - ", b" %d 103 - " % self._next(group, 2 ** 31), 1)
        return p


def parse(group, p):
    """-> (key, seq, modulus, ok). seq None = no counter present. ok False = corrupt (bad checksum / format)."""
    if p.startswith(b"UdPbC"):
        m = NM_TAG.match(p)
        if not m:
            return group, None, 1, False
        tag = m.group(1)
        ok = xor(tag) == int(m.group(2), 16)
        rest = m.group(3).rstrip(b"\r\n")
        i = rest.rfind(b"*")
        if i > 0 and rest[:1] in b"$!":
            ok = ok and xor(rest[1:i]) == int(rest[i + 1:i + 3] or b"0", 16)
        sfi = seq = None
        for x in tag.decode(errors="replace").split(","):
            if x.startswith("s:"):
                sfi = x[2:]
            elif x.startswith("n:") and x[2:].isdigit():
                seq = int(x[2:])
        return (group, sfi), seq, 999, ok
    if p[:5] in BINARY_TOKENS and len(p) >= 38:
        return group, struct.unpack(">I", p[24:28])[0], 2 ** 32 - 1, True
    if p.startswith(b"<"):
        m = SYSLOG_N.search(p)
        return (group, "syslog"), (int(m.group(2)) if m else None), 2 ** 31, bool(m)
    return group, None, 1, False


class Feed:
    """Human-readable live feed of messages (sampled: each group at most once per `gap` seconds per kind; losses/bad always) for Loki/Grafana."""

    def __init__(self, path, role, gap=2.0):
        self.f = open(path, "a", buffering=1)
        self.role, self.gap, self.last, self.lock = role, gap, {}, threading.Lock()

    def due(self, kind, group):
        now = time.monotonic()
        if now - self.last.get((kind, group), -1e9) < self.gap:
            return False
        self.last[(kind, group)] = now
        return True

    @staticmethod
    def text(p):
        if p.startswith((b"UdPbC", b"<")):
            return p.decode("ascii", "replace").replace("\0", "\\0").strip()
        return p[:40].hex(" ") + (" ..." if len(p) > 40 else "")

    def write(self, kind, group, where, info, p=b"", note=""):
        t = time.time()
        line = f"{self.role} {kind:<4} {group:<6} {time.strftime('%H:%M:%S', time.localtime(t))}.{int(t % 1 * 1000):03d} {where} {info} | {note}{self.text(p) if p else ''}"
        with self.lock:
            self.f.write(line + "\n")


class Gap:
    """Counts sequence gaps for one stream (handles wrap-around). Returns the number of newly lost items."""

    def __init__(self):
        self.last = None

    def feed(self, seq, mod):
        if seq is None:
            return 0
        if self.last is None:
            self.last = seq
            return 0
        d = (seq - self.last) % mod
        if d == 0 or d > mod // 2:        # duplicate or late/reordered: not a loss
            return 0
        self.last = seq
        return d - 1


class Endpoint:
    def __init__(self, a, sc):
        self.a, self.sc = a, sc
        self.role = a.role
        self.my_ip, self.peer_ip = (("192.168.50.1", "192.168.50.2") if a.role == "A" else ("192.168.50.2", "192.168.50.1"))
        if a.local:
            self.my_ip, self.peer_ip = (("127.0.0.1", "127.0.0.2") if a.role == "A" else ("127.0.0.2", "127.0.0.1"))
        self.my_ip = sc.get("tx_ip" if a.role == "A" else "rx_ip", self.my_ip) if not a.local else self.my_ip
        self.peer_ip = sc.get("rx_ip" if a.role == "A" else "tx_ip", self.peer_ip) if not a.local else self.peer_ip
        self.groups = {g["name"]: g for g in sc["groups"]}
        for g in self.groups.values():
            g["cos"] = s450.GROUPS[g["name"]][2]
            if not a.local:
                c.check_group(g["addr"])
        self.lock = threading.Lock()
        self.echo = bool(sc.get("echo", True))     # False: one-way test, the receiver only receives and counts (sends nothing back)
        self.vary = sc.get("vary") or {}       # {"streams": 0.25, "radar": 0.25, "mix": 0.64, "step_s": 2}: lowest fraction of the maximum rate per component
        self.rad = sc.get("radar")        # optional official radar stream: {"addr","port","cos","hz","bytes"}; counted as one more group "RADAR"
        if self.rad and not a.local:
            c.check_group(self.rad["addr"])
        names = list(self.groups) + (["RADAR"] if self.rad else [])
        self.tx = {g: {"msgs": 0, "bytes": 0} for g in names}
        self.rx = {g: {"msgs": 0, "bytes": 0, "lost": 0, "bad": 0, "wrong_dscp": 0, "foreign": 0} for g in names}
        self.gaps = {}
        self.builder = s450.Builder(False)
        self.stamper = Stamper()
        self.echo_q = {g: [] for g in self.groups}
        self.raw_q = {g: [] for g in self.groups}      # role A: unstamped payloads, filtered by the dashboard selection and stamped only when sent
        self.sel = selection.Selector()
        self.socks = {}
        self.t_start = time.time()
        self.feed = Feed(os.path.join(a.run_dir, f"feed_{a.role}.log"), a.role)
        self.path = "GE1>GE9" if a.role == "A" else "GE9>GE1"
        ctx = mp.get_context("fork")
        # unicast UDP streams that carry the bulk load (chart/database data): [{name, port, cos, mbps}]. Default: one "ONF" stream whose rate is target - mix.
        self.streams = [dict(x) for x in sc.get("streams")] if sc.get("streams") else [{"name": "ONF", "port": FILL_PORT, "cos": 1, "mbps": None}]
        # per stream: tx_pkts (sender) and one counter set per RECEIVER PROCESS (rx_procs processes share the port with SO_REUSEPORT, flows spread by source port)
        self.nat = {}                  # stream name -> {"rx": stats json path, "tx": stats json path or None} when the native program carries that stream
        self.sv = {x["name"]: {"tx_pkts": ctx.Value("q", 0, lock=False),
                               "rx": [{k: ctx.Value("q", 0, lock=False) for k in ("rx_pkts", "rx_bytes", "rx_lost")} for _ in range(int(x.get("rx_procs", 1)))]}
                   for x in self.streams}
        self.ctx = ctx

    # ---- sockets
    def tx_sock(self, cos):
        if cos not in self.socks:
            # marking "groups" (default): DSCP CS<cos> as test_sender.py; "none": every packet DSCP 0 (a multiplexer that cannot mark) so the switch must classify
            s = c.udp_socket(0 if self.sc.get("marking") == "none" else cos * 8)
            if not self.a.local:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.my_ip))
            s.bind((self.my_ip, 0))
            self.socks[cos] = s
        return self.socks[cos]

    def port(self, g):
        return g["port"] + 20000 if self.a.local and g["port"] < 1024 else g["port"]   # loopback self-test runs unprivileged

    def send_mix(self, group, payload):
        g = self.groups[group]
        dest = (self.peer_ip if self.a.local else g["addr"], self.port(g))
        self.tx_sock(g["cos"]).sendto(payload, dest)
        with self.lock:
            self.tx[group]["msgs"] += 1
            self.tx[group]["bytes"] += len(payload)
        if self.feed.due("TX", group):
            key, seq, _, _ = parse(group, payload)
            self.feed.write("TX", group, f"{g['addr']}:{g['port']}", f"{self.path} CS{g['cos']} n={seq} len={len(payload)}", payload)

    def next_payload(self, group):
        q = self.echo_q[group]
        if not q:
            q.extend(self.stamper.stamp(group, p) for p in s450.payloads(group, self.builder))
        return q.pop(0)

    def next_raw(self, group):
        q = self.raw_q[group]
        if not q:
            q.extend(s450.payloads(group, self.builder))
        return q.pop(0)

    def reload_selection(self):
        if self.sel.refresh():
            print(f"[{self.role}] selection changed: {len(self.sel.disabled)} item(s) switched off" + (f" ({', '.join(sorted(self.sel.disabled))[:200]})" if self.sel.disabled else ""), flush=True)

    # ---- threads
    def group_hz(self, name):
        """rounds per second of one group: the scenario's mix_hz unless the group has its own "hz" (e.g. VDR: 3 sentences per 5 s = 0.6)"""
        return float(self.groups[name].get("hz") or self.sc.get("mix_hz", 10))

    def mix_sender(self, stopper, t_end, t_start):
        stopper.sleep_until(t_start)
        t0 = time.monotonic()
        due = {name: t0 for name in self.groups}
        vr = Vary(self.vary.get("mix", 1.0), self.vary.get("step_s", 2.0))
        while time.monotonic() < t_end and not stopper.stopped():
            self.reload_selection()
            now = time.monotonic()
            fm = vr.factor(now)
            for name in self.groups:
                if now < due[name]:
                    continue
                for _ in range(int(self.groups[name].get("per_round", 1))):
                    p = self.next_raw(name)            # take it from the cycle even when switched off, so the others keep their rate
                    if self.sel.enabled(selection.key(name, p)):
                        self.send_mix(name, self.stamper.stamp(name, p))
                f = 1.0 if self.groups[name].get("hz") else fm      # a group with its own fixed rate (VDR) does not wobble
                due[name] = max(due[name] + 1.0 / (self.group_hz(name) * f), now - 1.0)
            time.sleep(max(0.0, min(0.02, min(due.values()) - time.monotonic())))

    def mix_receiver(self, stopper):
        sel = selectors.DefaultSelector()
        by_fd = {}
        for name, g in self.groups.items():
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_RECVTOS, 1)
            if self.a.local:
                s.bind((self.my_ip, self.port(g)))
            else:
                s.bind((g["addr"], g["port"]))
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(g["addr"]) + socket.inet_aton(self.my_ip))
            s.setblocking(False)
            sel.register(s, selectors.EVENT_READ, name)
        if self.rad:
            r = self.rad
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 23)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_RECVTOS, 1)
            if self.a.local:
                s.bind((self.my_ip, r["port"]))
            else:
                s.bind((r["addr"], r["port"]))
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(r["addr"]) + socket.inet_aton(self.my_ip))
            s.setblocking(False)
            sel.register(s, selectors.EVENT_READ, "RADAR")
        while not stopper.stopped():
            for key, _ in sel.select(0.2):
                name, s = key.data, key.fileobj
                for _ in range(64):
                    try:
                        data, anc, _, addr = s.recvmsg(65535, 64)
                    except BlockingIOError:
                        break
                    (self.on_radar if name == "RADAR" else self.on_mix)(*((data, anc, addr[0]) if name == "RADAR" else (name, data, anc, addr[0])))

    # ---- official radar stream (optional)
    def radar_send(self, payload):
        r = self.rad
        dest = (self.peer_ip if self.a.local else r["addr"], r["port"])
        self.tx_sock(r["cos"]).sendto(payload, dest)
        with self.lock:
            self.tx["RADAR"]["msgs"] += 1
            self.tx["RADAR"]["bytes"] += len(payload)

    def radar_sender(self, stopper, t_end, t_start):
        pk, _ = load_radar()
        hz = float(self.rad.get("hz") or 36.0)
        stopper.sleep_until(t_start)
        nxt, i = time.monotonic(), 0
        vr = Vary(self.vary.get("radar", 1.0), self.vary.get("step_s", 2.0))
        while time.monotonic() < t_end and not stopper.stopped():
            if not self.sel.enabled(selection.RADAR_KEY):     # switched off on the dashboard: pause (the counter carries on where it stopped)
                nxt = time.monotonic() + 1.0 / hz
                time.sleep(1.0 / hz)
                continue
            p = pk[i % len(pk)]
            self.radar_send(p)
            if self.feed.due("TX", "RADAR"):
                self.feed.write("TX", "RADAR", f"{self.rad['addr']}:{self.rad['port']}", f"{self.path} CS{self.rad['cos']} n={radar_seq(p)} len={len(p)}", p[:40])
            i += 1
            nxt += 1.0 / (hz * vr.factor(time.monotonic()))
            time.sleep(max(0.0, nxt - time.monotonic()))

    def on_radar(self, data, anc, src):
        r, cfg = self.rx["RADAR"], self.rad
        if src != self.peer_ip:
            with self.lock:
                r["foreign"] += 1
            return
        tos = next((struct.unpack("B", d[:1])[0] for lvl, t, d in anc if lvl == socket.IPPROTO_IP and t == socket.IP_TOS), None)
        seq, ok = radar_seq(data), len(data) == cfg.get("bytes", 17160)
        wrong = tos is not None and tos >> 5 != cfg["cos"]
        with self.lock:
            r["msgs"] += 1
            r["bytes"] += len(data)
            r["bad"] += 0 if ok else 1
            r["wrong_dscp"] += 1 if wrong else 0
            missed = self.gaps.setdefault(("RADAR", "radar"), Gap()).feed(seq, 128)
            r["lost"] += missed
        info = f"{'GE9>GE1' if self.role == 'A' else 'GE1>GE9'} CS{cfg['cos']} n={seq} len={len(data)}"
        where = f"{cfg['addr']}:{cfg['port']}"
        if missed:
            self.feed.write("LOSS", "RADAR", where, info, data[:40], f"MISSING {missed} before this one: ")
        if not ok:
            self.feed.write("BAD", "RADAR", where, info, data[:40], "UNEXPECTED SIZE: ")
        if self.feed.due("RX", "RADAR"):
            self.feed.write("RX", "RADAR", where, info, data[:40])
        if self.role == "B" and self.echo:           # pong: echo the datagram back verbatim on the same group
            self.radar_send(data)

    def on_mix(self, group, data, anc, src):
        r = self.rx[group]
        if src != self.peer_ip:
            with self.lock:
                r["foreign"] += 1
            return
        tos = next((struct.unpack("B", d[:1])[0] for lvl, t, d in anc if lvl == socket.IPPROTO_IP and t == socket.IP_TOS), None)
        key, seq, mod, ok = parse(group, data)
        g, rpath = self.groups[group], ("GE9>GE1" if self.role == "A" else "GE1>GE9")
        with self.lock:
            r["msgs"] += 1
            r["bytes"] += len(data)
            wrong = tos is not None and tos >> 5 != g["cos"]
            if not ok:
                r["bad"] += 1
            if wrong:
                r["wrong_dscp"] += 1
            missed = self.gaps.setdefault((group, key), Gap()).feed(seq, mod)
            r["lost"] += missed
        info = f"{rpath} CS{g['cos']} n={seq} len={len(data)}"
        if missed:
            self.feed.write("LOSS", group, f"{g['addr']}:{g['port']}", info, data, f"MISSING {missed} before this one: ")
        if not ok:
            self.feed.write("BAD", group, f"{g['addr']}:{g['port']}", info, data, "BAD CHECKSUM/FORMAT: ")
        if wrong and self.feed.due("DSCP", group):
            self.feed.write("DSCP", group, f"{g['addr']}:{g['port']}", f"{info} got_tos={tos:#04x} want=CS{g['cos']}", data)
        if self.feed.due("RX", group):
            self.feed.write("RX", group, f"{g['addr']}:{g['port']}", info, data)
        if self.role == "B" and self.echo:          # the "pong": answer every datagram with one of our own on the same group
            self.send_mix(group, self.next_payload(group))

    def fill_sender(self, stopper, t_end, pool, t_start, st):
        mbps, v = float(st["mbps"] or 0), self.sv[st["name"]]
        if mbps <= 0:
            return
        stopper.sleep_until(t_start)
        flows = max(1, int(st.get("flows", 1)))       # several source ports, each its own flow with its own sequence numbers (spread over the receiver processes)
        socks = []
        for _ in range(flows):
            s = c.udp_socket(0 if self.sc.get("marking") == "none" else int(st.get("cos", 1)) * 8)
            s.bind((self.my_ip, 0))
            socks.append(s)
        max_pps = mbps * 1e6 / 8 / (FILL_PAYLOAD + WIRE_OVERHEAD)        # mbps is the MAXIMUM; the actual rate wobbles below it (Vary)
        vr = Vary(self.vary.get("streams", 1.0), self.vary.get("step_s", 2.0))
        buf = bytearray(FILL_PAYLOAD)
        body = FILL_PAYLOAD - FILL_HDR.size
        dest = (self.peer_ip, int(st["port"]))
        seq, off, credit, last = 0, 0, 0.0, time.perf_counter()
        while time.monotonic() < t_end and not stopper.stopped():
            now = time.perf_counter()
            pps = max_pps * vr.factor(now)
            credit += (now - last) * pps
            last = now
            if credit > pps * 0.05 + 64:           # can't keep up: skip ahead instead of bursting
                credit = 64.0
            n = min(int(credit), 64)               # small batches: an even stream, not 256-packet bursts that overflow switch buffers
            for _ in range(n):
                fl = seq % flows
                FILL_HDR.pack_into(buf, 0, FILL_MAGIC + bytes([fl]), seq // flows, off)     # magic + flow id, per-flow sequence number, pool offset
                buf[FILL_HDR.size:] = pool[off:off + body]
                socks[fl].sendto(buf, dest)
                seq += 1
                off = (off + body) % (len(pool) - body)
            credit -= n
            v["tx_pkts"].value = seq
            if credit < 1:
                time.sleep(0.0003)

    def fill_receiver(self, stopper, st, k=0):
        """Receiver process k of this stream. rx_procs processes bind the same port (SO_REUSEPORT); the kernel hands each flow (source port) to one of them,
        so every process sees complete flows and can count gaps per flow."""
        v = self.sv[st["name"]]["rx"][k]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 24)
        s.bind((self.my_ip, int(st["port"])))
        s.settimeout(0.2)
        buf = bytearray(2048)
        gaps = {}
        pk = by = lost = 0
        last_pub = 0.0
        while not stopper.stopped():
            try:
                n = s.recv_into(buf)
            except socket.timeout:
                n = 0
            if n and n >= FILL_HDR.size and buf[:3] == FILL_MAGIC:
                pk += 1
                by += n
                lost += gaps.setdefault(buf[3], Gap()).feed(struct.unpack_from(">Q", buf, 4)[0], 2 ** 62)
            t = time.monotonic()
            if t - last_pub > 0.2:
                last_pub = t
                v["rx_pkts"].value, v["rx_bytes"].value, v["rx_lost"].value = pk, by, lost
        v["rx_pkts"].value, v["rx_bytes"].value, v["rx_lost"].value = pk, by, lost

    # ---- native streams
    @staticmethod
    def _read_json(path):
        try:
            return json.load(open(path))
        except (OSError, ValueError, TypeError):
            return {}

    def stream_counts(self, name):
        """(tx_pkts, rx_pkts, rx_bytes, rx_lost) of one stream, from the native program's json files or from the Python counters."""
        n = self.nat.get(name)
        if n:
            t, r = self._read_json(n["tx"]) if n.get("tx") else {}, self._read_json(n["rx"])
            return t.get("tx_pkts", 0), r.get("rx_pkts", 0), r.get("rx_bytes", 0), r.get("rx_lost", 0)
        v = self.sv[name]
        return v["tx_pkts"].value, sum(r["rx_pkts"].value for r in v["rx"]), sum(r["rx_bytes"].value for r in v["rx"]), sum(r["rx_lost"].value for r in v["rx"])

    def start_native(self, binary, pool, lead, dur, drain):
        """Starts one native receiver per stream (and one native sender per stream when this side sends). Returns the processes."""
        pool_path = os.path.join(c.PROJECT_ROOT, "data", "pool.bin")
        os.makedirs(os.path.dirname(pool_path), exist_ok=True)
        with open(pool_path, "wb") as f:
            f.write(pool)
        procs, vary = [], self.vary
        for st in self.streams:
            nm = st["name"]
            rx_json, tx_json = os.path.join(self.a.run_dir, f"native_{self.role}_{nm}_rx.json"), os.path.join(self.a.run_dir, f"native_{self.role}_{nm}_tx.json")
            self.nat[nm] = {"rx": rx_json, "tx": None}
            procs.append(subprocess.Popen([binary, "recv", "--ip", self.my_ip, "--port", str(st["port"]), "--run-for", f"{lead + dur + drain - 0.5:.1f}", "--stats", rx_json,
                                           "--stop-file", c.STOP_FILE], stderr=subprocess.PIPE))
            if self.echo or self.role == "A":
                self.nat[nm]["tx"] = tx_json
                cos = 0 if self.sc.get("marking") == "none" else int(st.get("cos", 1))
                procs.append(subprocess.Popen([binary, "send", "--my-ip", self.my_ip, "--peer-ip", self.peer_ip, "--port", str(st["port"]), "--mbps", str(st["mbps"]), "--cos", str(cos),
                                               "--pool", pool_path, "--start-in", f"{lead:.3f}", "--duration", f"{dur:.3f}", "--flows", str(int(st.get("flows", 1))),
                                               "--vary-min", str(vary.get("streams", 1.0)), "--vary-step", str(vary.get("step_s", 2.0)), "--stats", tx_json,
                                               "--stop-file", c.STOP_FILE], stderr=subprocess.PIPE))
        return procs

    # ---- stats
    def snapshot(self, final=False):
        sc_ = {x["name"]: self.stream_counts(x["name"]) for x in self.streams}
        with self.lock:
            return {"role": self.role, "ts": time.time(), "start": self.t_start, "final": final,
                    "scenario": self.sc["name"], "target_mbps": self.sc.get("target_mbps", 0), "filler_mbps": round(self.fill_mbps, 2), "duration_s": getattr(self, "duration_s", 0), "sent_secs": round(self.sent_secs(), 2),
                    "tx": {k: dict(v) for k, v in self.tx.items()}, "rx": {k: dict(v) for k, v in self.rx.items()},
                    "filler": {"tx_pkts": sum(c_[0] for c_ in sc_.values()), "tx_bytes": sum(c_[0] for c_ in sc_.values()) * FILL_PAYLOAD,
                               "rx_pkts": sum(c_[1] for c_ in sc_.values()), "rx_bytes": sum(c_[2] for c_ in sc_.values()), "rx_lost": sum(c_[3] for c_ in sc_.values())},
                    "streams": {x["name"]: {"port": x["port"], "mbps": round(float(x["mbps"] or 0), 3), "tx_pkts": sc_[x["name"]][0], "tx_bytes": sc_[x["name"]][0] * FILL_PAYLOAD,
                                            "rx_pkts": sc_[x["name"]][1], "rx_bytes": sc_[x["name"]][2], "rx_lost": sc_[x["name"]][3]} for x in self.streams}}

    def sent_secs(self):
        """Seconds the senders have actually been sending (a run stopped early reports its real length)."""
        t0, t1 = getattr(self, "t_send_start", None), getattr(self, "t_send_end", None)
        return 0.0 if t0 is None else max(0.0, min(time.monotonic(), t1) - t0)

    def write_stats(self, final=False):
        path = os.path.join(self.a.run_dir, f"stats_{self.role}.json")
        with open(path + ".tmp", "w") as f:
            json.dump(self.snapshot(final), f)
        os.replace(path + ".tmp", path)

    def run(self):
        sc = self.sc
        dur = min(float(self.a.duration or sc["duration_s"]), c.MAX_DURATION_S)
        drain = 5.0
        lead = max(0.0, (self.a.start_at or 0) - time.time())   # both endpoints start sending at the same instant, receivers are already up
        stopper = c.Stopper(lead + dur + drain)
        t_send_start = time.monotonic() + lead
        t_send_end = t_send_start + dur
        self.duration_s = dur
        self.t_send_start, self.t_send_end = t_send_start, t_send_end
        # estimate the mix's own wire rate so mix + filler = target
        probe, b = Stamper(), s450.Builder(False)
        per, per_sfi, sent_per_s = 0.0, {}, 0.0
        for n, g in self.groups.items():
            hz = self.group_hz(n)
            for p in s450.payloads(n, b):
                p = probe.stamp(n, p)
                per += (len(p) + WIRE_OVERHEAD) * 8 * hz / 1e6
                sent_per_s += hz
                m = NM_TAG.match(p)
                if m:
                    sfi = next((x for x in m.group(1).decode().split(",") if x.startswith("s:")), "s:?")
                    per_sfi[sfi] = per_sfi.get(sfi, 0) + hz
        worst = max(per_sfi.items(), key=lambda kv: kv[1])
        if worst[1] > MAX_SENTENCES_PER_SENSOR + 1e-9:
            raise SystemExit(f"REFUSING: sensor {worst[0][2:]} would send {worst[1]:.0f} sentences/s (limit {MAX_SENTENCES_PER_SENSOR}); lower mix_hz")
        self.sentences_per_s = sent_per_s
        rad_mbps = 0.0
        if self.rad:       # same on both sides (computed from the scenario) so each side's ONF dump gets the same remainder
            rad_mbps = radar_wire_bytes(self.rad.get("bytes", 17160)) * 8 * float(self.rad["hz"]) / 1e6
            per += rad_mbps
            if not sc.get("streams") and per > float(sc.get("target_mbps", 0)):
                raise SystemExit(f"REFUSING: mix + radar already need {c.rate(round(per, 2))}, above the target {c.rate(float(sc.get('target_mbps', 0)))}. Raise the rate.")
        self.mix_mbps = per
        if sc.get("streams"):      # payload profile: every stream has its own rate, the total is the sum
            self.fill_mbps = sum(float(x["mbps"]) for x in self.streams)
            sc["target_mbps"] = self.sc["target_mbps"] = round(per + self.fill_mbps, 2)
        else:
            self.fill_mbps = max(0.0, float(sc.get("target_mbps", 0)) - per)
            self.streams[0]["mbps"] = self.fill_mbps
        pool = load_pool(self.a.charts_dir)
        print(f"[{self.role}] {self.my_ip} -> {self.peer_ip}  {sc['name']}: mix{' (incl. radar ' + c.rate(round(rad_mbps, 2)) + ')' if self.rad else ''} {c.rate(round(per, 2))} + {' + '.join(x['name'] + ' ' + c.rate(round(float(x['mbps']), 1)) for x in self.streams) if sc.get('streams') else 'ONF dump ' + c.rate(round(self.fill_mbps, 1))} = {'total' if sc.get('streams') else 'target'} {c.rate(float(sc.get('target_mbps', 0)))}, {dur:.0f}s", flush=True)
        ts = [threading.Thread(target=self.mix_receiver, daemon=True, args=(stopper,))]
        if self.role == "A":
            ts.append(threading.Thread(target=self.mix_sender, daemon=True, args=(stopper, t_send_end, t_send_start)))
            if self.rad:
                ts.append(threading.Thread(target=self.radar_sender, daemon=True, args=(stopper, t_send_end, t_send_start)))
        # filler runs in separate processes: Python threads share one GIL and cannot do 200 Mbit/s of packets plus the mix
        binary, nprocs = native_binary(), []
        if binary:                                      # the bulk streams in the native program (see native_binary)
            nprocs = self.start_native(binary, pool, lead, dur, drain)
            print(f"[{self.role}] streams carried by the native program {binary}", flush=True)
        else:
            for st in self.streams:
                for k in range(int(st.get("rx_procs", 1))):
                    ts.append(self.ctx.Process(target=self.fill_receiver, daemon=True, args=(stopper, st, k)))
                if self.echo or self.role == "A":          # without echo only the sender sends the streams
                    ts.append(self.ctx.Process(target=self.fill_sender, daemon=True, args=(stopper, t_send_end, pool, t_send_start, st)))
        for t in ts:
            t.start()
        while not stopper.stopped():
            self.write_stats()
            time.sleep(1.0 - time.time() % 1.0)   # write on the wall-clock second so A's and B's snapshots line up (live counters match)
        for t in ts:
            t.join(timeout=3)
        for p in nprocs:                                # native programs end by themselves; stop any that are still running (early stop)
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.terminate()
                p.wait()
            err = p.stderr.read().decode(errors="replace").strip() if p.stderr else ""
            if p.returncode not in (0, None) or err:
                print(f"[{self.role}] native program exited {p.returncode}: {err[:200]}", flush=True)
        self.write_stats(final=True)
        print(f"[{self.role}] done", flush=True)


def load_pool(charts_dir, size=16 << 20):
    """Real S-57 chart bytes as filler payload (falls back to random bytes)."""
    pool = bytearray()
    try:
        files = sorted(f for f in os.listdir(charts_dir) if f.endswith(".json"))
        for f in files:
            with open(os.path.join(charts_dir, f), "rb") as fh:
                pool += fh.read(2 << 20)
            if len(pool) >= size:
                break
    except OSError:
        pass
    if len(pool) < 1 << 20:
        pool = bytearray(os.urandom(size))
    return bytes(pool[:size])


CTRL_PORT = 20999   # tiny handshake: A tells B "I start in N seconds", so the two PCs need no shared clock


def handshake(a, e):
    """Relative start. A (--start-in N) sends HELLO to B and starts N s later; B (--arm) waits for the HELLO, then starts N s after receiving it."""
    if a.role == "A":
        t0 = time.time()
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.bind((e.my_ip, 0))
        pkt = b"PPHELLO %.3f" % a.start_in
        if getattr(a, "announce", False):       # HELLO2: also carry run name, duration and the scenario itself (receiver needs no arguments)
            blob = base64.b64encode(zlib.compress(json.dumps(e.sc).encode())).decode()
            pkt = f"PPHELLO2 {a.start_in:.3f} {a.duration or e.sc['duration_s']:.1f} {os.path.basename(a.run_dir.rstrip('/'))} {blob}".encode()
        for _ in range(8):                      # a few copies, in case the first is lost
            sk.sendto(pkt, (e.peer_ip, CTRL_PORT))
            time.sleep(0.1)
        a.start_at = t0 + a.start_in
        print(f"[A] HELLO sent, both ends start in {a.start_in - (time.time() - t0):.1f} s", flush=True)
    else:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.bind((e.my_ip, CTRL_PORT))
        sk.settimeout(1.0)
        deadline = time.time() + a.arm_timeout
        print(f"[B] armed: waiting up to {a.arm_timeout:.0f} s for the sender's HELLO on {e.my_ip}:{CTRL_PORT}", flush=True)
        while True:
            if os.path.exists(c.STOP_FILE) or time.time() > deadline:
                raise SystemExit("[B] no HELLO from the sender (stopped or timed out)")
            try:
                data, src = sk.recvfrom(100)
            except socket.timeout:
                continue
            if data.startswith(b"PPHELLO "):
                a.start_at = time.time() + float(data.split()[1]) - 0.001
                print(f"[B] HELLO from {src[0]}: starting in {float(data.split()[1]):.1f} s", flush=True)
                break
    sk.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--role", choices=["A", "B"], required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--start-at", type=float, help="epoch seconds at which senders start")
    ap.add_argument("--start-in", type=float, help="role A: start N seconds after launch and tell B (no clock sync needed; B runs with --arm)")
    ap.add_argument("--announce", action="store_true", help="role A with --start-in: put run name, duration and scenario into the HELLO (for traffic.receiver)")
    ap.add_argument("--arm", action="store_true", help="role B: wait for A's HELLO, then start with it")
    ap.add_argument("--arm-timeout", type=float, default=900.0)
    ap.add_argument("--local", action="store_true", help="loopback self-test: unicast between 127.0.0.1 and 127.0.0.2")
    ap.add_argument("--charts-dir", default=next((p for p in (os.path.join(c.PROJECT_ROOT, "samples", "charts", "charts_sample"), os.path.join(c.PROJECT_ROOT, "samples", "charts_pool")) if os.path.isdir(p)), None))
    a = ap.parse_args()
    e = Endpoint(a, json.load(open(a.scenario)))
    if a.start_in is not None or a.arm:
        handshake(a, e)
    e.run()


if __name__ == "__main__":
    main()
