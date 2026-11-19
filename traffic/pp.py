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
Definitions used in the metrics:
  message = one 450 sample (sentence / binary block / syslog line) from the mix
  packet  = any UDP datagram (mix + filler)         data = payload bytes of those
Stats are written to <run-dir>/stats_<role>.json every second (read by monitor/pp_exporter.py).
Lab only: multicast TTL 1, 239.0.0.0/8 only, kill switch .STOP (scripts/estop.sh), 900 s cap.
"""
import argparse
import base64
import json
import os
import re
import selectors
import socket
import struct
import multiprocessing as mp
import threading
import time
import zlib

from . import common as c
from . import sender450 as s450

FILL_PORT = 20100            # CCRS JSON port from the sheet: the ONF dump (no 450 header) is the load filler
FILL_PAYLOAD = 1400          # UDP payload bytes of one filler packet (wire = +46: Eth+FCS 18, IP 20, UDP 8)
FILL_HDR = struct.Struct(">4sQI")   # magic, seq, pool offset
WIRE_OVERHEAD = 46
MAX_SENTENCES_PER_SENSOR = 13   # one sensor (talker/SFI) may send at most 13 sentences per second
BINARY_TOKENS = (b"NkPgN", b"RaUdP", b"RrUdP")
NM_TAG = re.compile(rb"^UdPbC\x00\\([^*\\]*)\*([0-9A-Fa-f]{2})\\(.*)$", re.S)
SYSLOG_N = re.compile(rb"450-(\S+) (\d+) 103 ")


def xor(b):
    x = 0
    for ch in b:
        x ^= ch
    return x


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
        self.tx = {g: {"msgs": 0, "bytes": 0} for g in self.groups}
        self.rx = {g: {"msgs": 0, "bytes": 0, "lost": 0, "bad": 0, "wrong_dscp": 0, "foreign": 0} for g in self.groups}
        self.gaps = {}
        self.builder = s450.Builder(False)
        self.stamper = Stamper()
        self.echo_q = {g: [] for g in self.groups}
        self.socks = {}
        self.t_start = time.time()
        self.feed = Feed(os.path.join(a.run_dir, f"feed_{a.role}.log"), a.role)
        self.path = "GE1>GE9" if a.role == "A" else "GE9>GE1"
        ctx = mp.get_context("fork")
        self.fv = {k: ctx.Value("q", 0, lock=False) for k in ("tx_pkts", "rx_pkts", "rx_bytes", "rx_lost")}
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

    # ---- threads
    def mix_sender(self, stopper, t_end, t_start):
        hz = float(self.sc.get("mix_hz", 10))
        stopper.sleep_until(t_start)
        nxt = time.monotonic()
        while time.monotonic() < t_end and not stopper.stopped():
            for name in self.groups:
                for _ in range(int(self.groups[name].get("per_round", 1))):
                    self.send_mix(name, self.next_payload(name))
            nxt += 1.0 / hz
            time.sleep(max(0.0, nxt - time.monotonic()))

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
        while not stopper.stopped():
            for key, _ in sel.select(0.2):
                name, s = key.data, key.fileobj
                for _ in range(64):
                    try:
                        data, anc, _, addr = s.recvmsg(4096, 64)
                    except BlockingIOError:
                        break
                    self.on_mix(name, data, anc, addr[0])

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
        if self.role == "B":                        # the "pong": answer every datagram with one of our own on the same group
            self.send_mix(group, self.next_payload(group))

    def fill_sender(self, stopper, t_end, pool, t_start):
        mbps = self.fill_mbps
        if mbps <= 0:
            return
        stopper.sleep_until(t_start)
        s = c.udp_socket(0 if self.sc.get("marking") == "none" else "CS1")
        s.bind((self.my_ip, 0))
        pps = mbps * 1e6 / 8 / (FILL_PAYLOAD + WIRE_OVERHEAD)
        buf = bytearray(FILL_PAYLOAD)
        body = FILL_PAYLOAD - FILL_HDR.size
        dest = (self.peer_ip, FILL_PORT)
        seq, off, t0 = 0, 0, time.perf_counter()
        while time.monotonic() < t_end and not stopper.stopped():
            due = int((time.perf_counter() - t0) * pps)
            if due - seq > pps * 0.05:            # can't keep up: skip ahead instead of bursting
                seq = due - 256
            for _ in range(min(due - seq, 256)):
                FILL_HDR.pack_into(buf, 0, b"FILL", seq, off)
                buf[FILL_HDR.size:] = pool[off:off + body]
                s.sendto(buf, dest)
                seq += 1
                off = (off + body) % (len(pool) - body)
            self.fv["tx_pkts"].value = seq
            time.sleep(0.0005)

    def fill_receiver(self, stopper):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 23)
        s.bind((self.my_ip, FILL_PORT))
        s.settimeout(0.2)
        buf = bytearray(2048)
        gap = Gap()
        pk = by = lost = 0
        last_pub = 0.0
        while not stopper.stopped():
            try:
                n = s.recv_into(buf)
            except socket.timeout:
                n = 0
            if n and n >= FILL_HDR.size and buf[:4] == b"FILL":
                pk += 1
                by += n
                lost += gap.feed(struct.unpack_from(">Q", buf, 4)[0], 2 ** 62)
            t = time.monotonic()
            if t - last_pub > 0.2:
                last_pub = t
                self.fv["rx_pkts"].value, self.fv["rx_bytes"].value, self.fv["rx_lost"].value = pk, by, lost
        self.fv["rx_pkts"].value, self.fv["rx_bytes"].value, self.fv["rx_lost"].value = pk, by, lost

    # ---- stats
    def snapshot(self, final=False):
        with self.lock:
            return {"role": self.role, "ts": time.time(), "start": self.t_start, "final": final,
                    "scenario": self.sc["name"], "target_mbps": self.sc.get("target_mbps", 0), "filler_mbps": round(self.fill_mbps, 2), "duration_s": getattr(self, "duration_s", 0), "sent_secs": round(self.sent_secs(), 2),
                    "tx": {k: dict(v) for k, v in self.tx.items()}, "rx": {k: dict(v) for k, v in self.rx.items()},
                    "filler": {"tx_pkts": self.fv["tx_pkts"].value, "tx_bytes": self.fv["tx_pkts"].value * FILL_PAYLOAD,
                               "rx_pkts": self.fv["rx_pkts"].value, "rx_bytes": self.fv["rx_bytes"].value, "rx_lost": self.fv["rx_lost"].value}}

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
        sizes, per_sfi = [], {}
        for n, g in self.groups.items():
            for p in s450.payloads(n, b):
                p = probe.stamp(n, p)
                sizes.append(len(p) + WIRE_OVERHEAD)
                m = NM_TAG.match(p)
                if m:
                    sfi = next((x for x in m.group(1).decode().split(",") if x.startswith("s:")), "s:?")
                    per_sfi[sfi] = per_sfi.get(sfi, 0) + 1
        worst = max(per_sfi.items(), key=lambda kv: kv[1])
        if worst[1] * float(sc.get("mix_hz", 10)) > MAX_SENTENCES_PER_SENSOR + 1e-9:
            raise SystemExit(f"REFUSING: sensor {worst[0][2:]} would send {worst[1] * float(sc.get('mix_hz', 10)):.0f} sentences/s (limit {MAX_SENTENCES_PER_SENSOR}); lower mix_hz")
        per = sum(sizes) * 8 * float(sc.get("mix_hz", 10)) / 1e6
        self.mix_mbps = per
        self.fill_mbps = max(0.0, float(sc.get("target_mbps", 0)) - per)
        pool = load_pool(self.a.charts_dir)
        print(f"[{self.role}] {self.my_ip} -> {self.peer_ip}  {sc['name']}: mix {c.rate(round(per, 2))} + ONF dump {c.rate(round(self.fill_mbps, 1))} = target {c.rate(float(sc.get('target_mbps', 0)))}, {dur:.0f}s", flush=True)
        ts = [threading.Thread(target=self.mix_receiver, daemon=True, args=(stopper,))]
        if self.role == "A":
            ts.append(threading.Thread(target=self.mix_sender, daemon=True, args=(stopper, t_send_end, t_send_start)))
        # filler runs in separate processes: Python threads share one GIL and cannot do 200 Mbit/s of packets plus the mix
        ts += [self.ctx.Process(target=self.fill_receiver, daemon=True, args=(stopper,)),
               self.ctx.Process(target=self.fill_sender, daemon=True, args=(stopper, t_send_end, pool, t_send_start))]
        for t in ts:
            t.start()
        while not stopper.stopped():
            self.write_stats()
            time.sleep(1.0 - time.time() % 1.0)   # write on the wall-clock second so A's and B's snapshots line up (live counters match)
        for t in ts:
            t.join(timeout=3)
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
