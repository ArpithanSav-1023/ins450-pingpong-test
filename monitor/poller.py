"""SNMP poller + Prometheus exporter for the CBS350.

- Polls IF-MIB (ifTable + ifXTable) with snmpbulkwalk (v2c or v3 authPriv).
- Stores RAW counters (table raw_samples) separately from DERIVED metrics (derived_samples).
- Derives per-port RX/TX bps and utilisation from ifHC* octet deltas.
- Invalid deltas (reboot, counter reset, timeout, interface speed change) are skipped, never computed.
- Read-only: only GET/GETBULK, never SET.
"""
import argparse
import os
import sqlite3
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from prometheus_client import Gauge, start_http_server

IFX = "1.3.6.1.2.1.31.1.1.1"
IFT = "1.3.6.1.2.1.2.2.1"
UPTIME = "1.3.6.1.2.1.1.3.0"

# column name -> (table oid prefix, column number)
# NOTE: on CBS350 fw 3.6.0.3 the 64-bit packet counters (ifHC*Pkts) always read 0; the 64-bit OCTET counters work.
# Packet counts therefore use the legacy 32-bit ifTable counters (in/out unicast and non-unicast = multicast+broadcast).
COLS = {
    "name": (IFX, 1),
    "hc_in_octets": (IFX, 6), "hc_out_octets": (IFX, 10),
    "high_speed_mbps": (IFX, 15),
    "admin": (IFT, 7), "oper": (IFT, 8),
    "in_ucast": (IFT, 11), "in_nucast": (IFT, 12), "out_ucast": (IFT, 17), "out_nucast": (IFT, 18),
    "in_discards": (IFT, 13), "in_errors": (IFT, 14), "out_discards": (IFT, 19), "out_errors": (IFT, 20),
}
COUNTERS = ["hc_in_octets", "hc_out_octets", "in_ucast", "in_nucast", "out_ucast", "out_nucast",
            "in_discards", "in_errors", "out_discards", "out_errors"]
COUNTERS32 = set(COUNTERS) - {"hc_in_octets", "hc_out_octets"}  # 32-bit: can genuinely wrap at 2**32
MAX_PORT_INDEX = 28  # gi1..gi28 on CBS350-24T-4G; Po/tunnel/loopback indexes are ignored


def snmp_args(a):
    if a.version == "3":
        return ["-v3", "-l", "authPriv", "-u", os.environ["SNMPV3_USER"],
                "-a", "SHA", "-A", os.environ["SNMPV3_AUTH_PASS"],
                "-x", "AES", "-X", os.environ["SNMPV3_PRIV_PASS"]]
    return ["-v2c", "-c", a.community]


def walk(a, oid):
    cmd = ["snmpbulkwalk", "-Oqn", "-Cr50", "-t", "2", "-r", "1", *snmp_args(a), a.host, oid]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200] or "snmpbulkwalk failed")
    return out.stdout.splitlines()


def get_uptime(a):
    cmd = ["snmpget", "-Oqnv", "-Ot", "-t", "2", "-r", "1", *snmp_args(a), a.host, UPTIME]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip()[:200] or "snmpget failed")
    return int(out.stdout.strip())  # timeticks (1/100 s)


def poll(a):
    """Return (uptime_ticks, {ifIndex: {col: value}}) for physical ports only.

    One bulkwalk per needed column (a full ifXTable walk is ~1.5 s on this switch; single columns ~0.1 s),
    run in parallel so a whole poll takes ~0.2 s.
    """
    with ThreadPoolExecutor(max_workers=len(COLS) + 1) as ex:
        up_f = ex.submit(get_uptime, a)
        futs = {col: ex.submit(walk, a, f"{tbl}.{num}") for col, (tbl, num) in COLS.items()}
        uptime = up_f.result()
        rows = {}
        for col, (tbl, num) in COLS.items():
            prefix = f"{tbl}.{num}."
            for line in futs[col].result():
                oid, _, val = line.partition(" ")
                oid = oid.lstrip(".")
                if oid.startswith(prefix):
                    idx = int(oid[len(prefix):])
                    if idx <= MAX_PORT_INDEX:
                        rows.setdefault(idx, {})[col] = val.strip().strip('"')
    return uptime, rows


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def init_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS raw_samples (ts REAL, uptime INTEGER, if_index INTEGER, name TEXT, col TEXT, value INTEGER)")
    db.execute("CREATE TABLE IF NOT EXISTS derived_samples (ts REAL, if_index INTEGER, name TEXT, metric TEXT, value REAL)")
    db.execute("CREATE INDEX IF NOT EXISTS ix_raw ON raw_samples(ts, if_index)")
    db.execute("CREATE INDEX IF NOT EXISTS ix_der ON derived_samples(ts, if_index)")
    return db


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="192.168.1.254")
    p.add_argument("--version", choices=["2c", "3"], default="2c")
    p.add_argument("--community", default=os.environ.get("SNMP_COMMUNITY", "hello"))
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--port", type=int, default=9101)
    p.add_argument("--db", default="data/metrics.sqlite")
    a = p.parse_args()

    os.makedirs(os.path.dirname(a.db), exist_ok=True)
    db = init_db(a.db)
    L = ["port", "if_index"]
    g = {
        "rx_bps": Gauge("cbs_port_rx_bps", "RX bits/s", L), "tx_bps": Gauge("cbs_port_tx_bps", "TX bits/s", L),
        "rx_util": Gauge("cbs_port_rx_util_percent", "RX utilisation %", L),
        "tx_util": Gauge("cbs_port_tx_util_percent", "TX utilisation %", L),
        "speed": Gauge("cbs_port_speed_bps", "Interface speed bps", L),
        "up": Gauge("cbs_port_oper_up", "1 if ifOperStatus up", L),
        "poll_ok": Gauge("cbs_poll_success", "1 if last poll succeeded"),
        "poll_dur": Gauge("cbs_poll_duration_seconds", "Last poll duration"),
        "skipped": Gauge("cbs_poll_skipped_deltas_total", "Deltas skipped as invalid", ["reason"]),
    }
    raw_g = {c: Gauge(f"cbs_port_raw_{c}", f"Raw {c}", L) for c in COUNTERS}
    skipped = {"reboot": 0, "reset": 0, "timeout": 0, "speed_change": 0}
    start_http_server(a.port, addr="127.0.0.1")
    print(f"[poller] {a.host} snmp v{a.version} every {a.interval}s, metrics on :{a.port}", flush=True)

    prev = None  # (mono_time, uptime, rows)
    while True:
        t0 = time.monotonic()
        wall = time.time()
        try:
            uptime, rows = poll(a)
            g["poll_ok"].set(1)
        except Exception as e:  # timeout / auth failure: skip, do NOT compute from stale data
            print(f"[poller] poll failed: {e}", flush=True)
            g["poll_ok"].set(0)
            skipped["timeout"] += 1
            g["skipped"].labels("timeout").set(skipped["timeout"])
            prev = None  # discontinuity: next delta would span an unknown gap
            time.sleep(a.interval)
            continue
        now = time.monotonic()
        g["poll_dur"].set(now - t0)

        reboot = prev is not None and uptime < prev[1]
        if reboot:
            skipped["reboot"] += 1
            g["skipped"].labels("reboot").set(skipped["reboot"])

        raw_batch, der_batch = [], []
        for idx, r in rows.items():
            name = r.get("name", str(idx))
            lbl = (name, str(idx))
            spd_mbps = to_int(r.get("high_speed_mbps")) or 0
            cap = spd_mbps * 1_000_000
            g["speed"].labels(*lbl).set(cap)
            g["up"].labels(*lbl).set(1 if r.get("oper") in ("1", "up", "up(1)") else 0)
            for c in COUNTERS:
                v = to_int(r.get(c))
                if v is not None:
                    raw_g[c].labels(*lbl).set(v)
                    raw_batch.append((wall, uptime, idx, name, c, v))

            if prev is None or reboot or idx not in prev[2]:
                continue
            dt = now - prev[0]
            pr = prev[2][idx]
            if dt <= 0:
                continue
            if to_int(pr.get("high_speed_mbps")) != spd_mbps:
                skipped["speed_change"] += 1
                g["skipped"].labels("speed_change").set(skipped["speed_change"])
                continue
            deltas = {}
            bad = False
            for c in COUNTERS:
                cur, old = to_int(r.get(c)), to_int(pr.get(c))
                if cur is None or old is None:
                    continue
                if cur < old and c in COUNTERS32 and old > 2**32 - 2**26:
                    cur += 2**32  # genuine 32-bit wrap (previous value was within 64M of the top)
                elif cur < old:  # counter reset / discontinuity: do not derive
                    bad = True
                    break
                deltas[c] = cur - old
            if bad:
                skipped["reset"] += 1
                g["skipped"].labels("reset").set(skipped["reset"])
                continue
            rx_bps = deltas.get("hc_in_octets", 0) * 8 / dt
            tx_bps = deltas.get("hc_out_octets", 0) * 8 / dt
            g["rx_bps"].labels(*lbl).set(rx_bps)
            g["tx_bps"].labels(*lbl).set(tx_bps)
            # RX and TX are independent full-duplex directions: never summed against one capacity
            rx_u = rx_bps / cap * 100 if cap else 0.0
            tx_u = tx_bps / cap * 100 if cap else 0.0
            g["rx_util"].labels(*lbl).set(rx_u)
            g["tx_util"].labels(*lbl).set(tx_u)
            for m, v in (("rx_bps", rx_bps), ("tx_bps", tx_bps), ("rx_util_pct", rx_u), ("tx_util_pct", tx_u)):
                der_batch.append((wall, idx, name, m, v))
            for c, d in deltas.items():
                der_batch.append((wall, idx, name, f"{c}_per_s", d / dt))

        db.executemany("INSERT INTO raw_samples VALUES (?,?,?,?,?,?)", raw_batch)
        db.executemany("INSERT INTO derived_samples VALUES (?,?,?,?,?)", der_batch)
        db.commit()
        prev = (now, uptime, rows)
        time.sleep(max(0.0, a.interval - (time.monotonic() - t0)))


if __name__ == "__main__":
    main()
