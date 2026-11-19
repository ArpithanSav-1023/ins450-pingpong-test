"""Shared helpers: kill switch, pacing, DSCP, sockets, CSV logs.

Safety features (lab only):
  * Kill switch: `touch <project>/.STOP` (or scripts/estop.sh) stops every sender and the receiver.
  * Hard duration cap (MAX_DURATION_S) regardless of what a scenario asks for.
  * Every sender refuses to run if its nominal rate exceeds its configured ceiling (see controller).
  * Unicast only: no broadcast, no multicast.
"""
import argparse
import csv
import ipaddress
import json
import os
import signal
import socket
import threading
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Kept inside the project folder (not /tmp): files made by the root-run controller stay deletable by you.
STOP_FILE = os.environ.get("SNMP_DEMO_STOP_FILE", os.path.join(PROJECT_ROOT, ".STOP"))
MAX_DURATION_S = 3600


def rate(mbit):
    """'50 Mbit/s (6.25 MB/s)': every place that shows a rate shows megabytes per second too (1 MB/s = 8 Mbit/s)."""
    return f"{mbit:g} Mbit/s ({mbit / 8:.3g} MB/s)"

# DSCP code points (decimal). IP TOS byte = DSCP << 2.
DSCP = {"CS0": 0, "CS1": 8, "CS2": 16, "CS3": 24, "CS4": 32, "AF11": 10, "AF21": 18, "AF31": 26, "AF41": 34, "CS5": 40, "EF": 46, "CS6": 48}

PORTS = {
    "navd_udp": 5001, "navd_tcp": 5002,
    "sat_tel_udp": 5011, "sat_tel_tcp": 5012, "sat_bulk_tcp": 5013,
    "cam_udp": 5021,
}

TX_HEADER = ["ts_ns", "stream", "seq", "bytes", "ok", "transport", "frame", "idx"]
RX_HEADER = ["recv_ns", "stream", "seq", "sent_ns", "bytes", "transport", "frame", "idx"]


def now_ns():
    """Monotonic ns. Sender and receiver share one host clock, so one-way latency is valid."""
    return time.monotonic_ns()


def dscp_value(v):
    return DSCP[v] if isinstance(v, str) else int(v)


def wire_mbps(payload_bytes, pps, transport="udp"):
    """Approximate on-wire Mbit/s including Ethernet+IP+L4 headers."""
    overhead = 46 if transport == "udp" else 58
    return (payload_bytes + overhead) * 8 * pps / 1e6


class Stopper:
    """Stops on SIGINT/SIGTERM, the STOP file, or the duration deadline."""

    def __init__(self, duration_s):
        self._flag = threading.Event()
        self.deadline = time.monotonic() + min(float(duration_s), MAX_DURATION_S)
        self._last = 0.0
        for s in (signal.SIGINT, signal.SIGTERM):
            signal.signal(s, lambda *_: self._flag.set())

    def stopped(self):
        if self._flag.is_set():
            return True
        t = time.monotonic()
        if t >= self.deadline:
            self._flag.set()
            return True
        if t - self._last > 0.1:
            self._last = t
            if os.path.exists(STOP_FILE):
                self._flag.set()
                return True
        return False

    def sleep_until(self, mono_deadline):
        """Sleep until the deadline; return False if we were stopped first."""
        while True:
            if self.stopped():
                return False
            rem = mono_deadline - time.monotonic()
            if rem <= 0:
                return True
            time.sleep(min(rem, 0.05))


def paced(stopper, hz):
    """Yield at `hz` using absolute deadlines (no drift). Resyncs if we fall >0.5 s behind."""
    period = 1.0 / hz
    nxt = time.monotonic()
    while True:
        if not stopper.sleep_until(nxt):
            return
        yield
        nxt += period
        now = time.monotonic()
        if nxt < now - 0.5:
            nxt = now


class CsvLog:
    """Thread-safe CSV writer. A background thread flushes every 0.5 s so live viewers can tail the file."""

    def __init__(self, path, header):
        self._f = open(path, "w", newline="", buffering=1 << 16)
        self._w = csv.writer(self._f)
        self._w.writerow(header)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        threading.Thread(target=self._flusher, daemon=True).start()

    def _flusher(self):
        while not self._stop.wait(0.5):
            with self._lock:
                if not self._f.closed:
                    self._f.flush()

    def row(self, *r):
        with self._lock:
            self._w.writerow(r)

    def close(self):
        self._stop.set()
        with self._lock:
            if not self._f.closed:
                self._f.flush()
                self._f.close()


def udp_socket(dscp, sndbuf=1 << 21):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, dscp_value(dscp) << 2)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, sndbuf)
    return s


def check_group(group):
    """Only administratively-scoped (239.0.0.0/8) multicast groups are allowed in this lab."""
    ip = ipaddress.ip_address(group)
    if not (ip.is_multicast and ip in ipaddress.ip_network("239.0.0.0/8")):
        raise ValueError(f"multicast group {group} is not in 239.0.0.0/8")
    return group


def mcast_socket(dscp, iface_ip, ttl=1):
    """UDP socket for sending multicast: TTL 1 (never routed), no loopback to the sender, fixed egress interface."""
    s = udp_socket(dscp)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
    return s


def tcp_connect(target, port, dscp, stopper, timeout=5.0):
    """Connect with retry until stopped. Returns a socket or None."""
    while not stopper.stopped():
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, dscp_value(dscp) << 2)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(timeout)
        try:
            s.connect((target, port))
            return s
        except OSError:
            s.close()
            stopper.sleep_until(time.monotonic() + 0.5)
    return None


def source_main(run_fn):
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--duration", type=float, required=True)
    a = ap.parse_args()
    with open(a.config) as f:
        cfg = json.load(f)
    run_fn(cfg, a.run_dir, a.target, Stopper(a.duration))
