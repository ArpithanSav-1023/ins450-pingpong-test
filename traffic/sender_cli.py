"""SENDER side (the separate PC). You choose only the rate and the duration; the receiver follows by itself.
    python3 -m traffic.sender_cli --rate 50 --minutes 10
Writes runs/<run>/scenario.json (from scenarios/pp9_025mbps.json with your rate and duration), then runs endpoint A with the HELLO handshake."""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys

from . import common as c

ROOT = c.PROJECT_ROOT


# THE PAYLOAD (MAXIMUM Mbit/s on the wire, per direction; the actual rate varies below it): radar 0.65, ECDIS 60, ONF database 500, other ONF 25, all 450 sentences ~64-100/s, VDR 3 sentences per 5 s.
# The ONF streams and ECDIS are unicast UDP with real chart bytes (CS1, best effort), each on its own port (ECDIS is assumed to be the same kind of stream).
PAYLOAD = {"radar_mbps": 0.65, "vdr_hz": 0.6,
           "streams": [{"name": "ECDIS", "mbps": 60, "port": 20101, "cos": 1},
                       {"name": "ONF_DB", "mbps": 500, "port": 20100, "cos": 1},   # "flows": 6, "rx_procs": 3 splits it over processes: tried, did not help on the i3 receiver
                       {"name": "ONF_OTHER", "mbps": 25, "port": 20102, "cos": 1}]}


def nic_speed_mbps(ip="192.168.50.1"):
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr"], capture_output=True, text=True).stdout
        nic = next(l.split()[1] for l in out.splitlines() if ip + "/" in l)
        return int(open(f"/sys/class/net/{nic}/speed").read()), nic
    except Exception:
        return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", required=True, help='offered load per direction, Mbit/s on the wire (mix + ONF dump), or the word "payload" for the fixed payload profile (PAYLOAD below)')
    ap.add_argument("--sentences", type=float, default=100, help="payload profile: MAXIMUM of all 450 sentences together per second (default 100; the rate wobbles between 64 and this)")
    ap.add_argument("--echo", action="store_true", help="payload profile: the receiver echoes everything back (default for the payload: no echo, one way)")
    ap.add_argument("--steady", action="store_true", help="payload profile: send every stream constantly at its maximum instead of varying it")
    ap.add_argument("--minutes", type=float, help="run length in minutes")
    ap.add_argument("--seconds", type=float, help="run length in seconds (instead of --minutes)")
    ap.add_argument("--base", default=os.path.join(ROOT, "scenarios", "pp9_025mbps.json"))
    ap.add_argument("--no-radar", action="store_true", help="leave the official radar stream out of the mix")
    a = ap.parse_args()
    payload = a.rate.lower() in ("payload", "p")
    if not payload:
        try:
            a.rate = float(a.rate)
        except ValueError:
            sys.exit('--rate must be a number (Mbit/s) or the word "payload"')
    secs = a.seconds if a.seconds else (a.minutes or 10) * 60
    if secs > c.MAX_DURATION_S:
        sys.exit(f"duration above the safety cap of {c.MAX_DURATION_S // 60} min (traffic/common.py MAX_DURATION_S)")
    if payload:
        a.rate = sum(x["mbps"] for x in PAYLOAD["streams"]) + PAYLOAD["radar_mbps"] + 0.2      # rough total (pp.py computes the exact one) for the NIC check
    if a.rate > 1000:
        sys.exit("REFUSING: target above 1000 Mbit/s")
    speed, nic = nic_speed_mbps()
    if speed and a.rate > 0.97 * speed:
        sys.exit(f"{c.rate(a.rate)} does not fit through {nic} ({c.rate(speed)} link, about {c.rate(round(0.97 * speed))} possible). Lower the rate.")
    sc = json.load(open(a.base))
    tag = f"{a.rate:g}".replace(".", "p")
    if not a.no_radar:      # official HALO20 radar stream, replayed on its own multicast group (see tools/extract_radar.py)
        from . import pp
        if not os.path.exists(pp.RADAR_FILE):
            sys.exit(f"radar slice {pp.RADAR_FILE} not found. Build it once: .venv/bin/python tools/extract_radar.py /home/sav1023/Code/test/radar_dump.pcapng   (or use --no-radar)")
        _, hz = pp.load_radar()
        if payload:       # radar 650 kbit/s on the wire: the captured spokes, spaced out
            hz = PAYLOAD["radar_mbps"] * 1e6 / 8 / pp.radar_wire_bytes(17160)
        sc["radar"] = {"addr": "239.6.7.8", "port": 6678, "cos": 2, "bytes": 17160, "hz": round(hz, 3),
                       "_note": "official HALO20 radar spoke stream (captured on 236.6.7.8:6678, moved into 239/8), 17160-byte datagrams, IP-fragmented"}
    mine = os.environ.get("PP_PORT", "GE9")                 # the switch port THIS (sending) PC is on
    other = "GE1" if mine == "GE9" else "GE9"
    sc.update({"name": f"pp9_{int(a.rate):03d}mbps" if a.rate == int(a.rate) else f"pp9_{tag}mbps", "target_mbps": a.rate, "duration_s": secs,
               "a_port": mine, "b_port": other})
    if payload:
        per_round = sum(int(g.get("per_round", 1)) for g in sc["groups"] if g["name"] != "VDRD")
        for g in sc["groups"]:
            if g["name"] == "VDRD":
                g["hz"] = PAYLOAD["vdr_hz"]                      # VDR: 3 sentences per 5 s
        sc.update({"name": "payload", "profile": "payload", "mix_hz": round(a.sentences / per_round, 4), "streams": [dict(x) for x in PAYLOAD["streams"]]})
        sc.pop("marking", None)
        sc["echo"] = bool(a.echo)
        if not a.steady:       # the payload numbers are MAXIMUMS: every component varies below them (new random level every step_s seconds)
            sc["vary"] = {"streams": 0.25, "radar": 0.25, "mix": round(min(64 / a.sentences, 1.0), 3), "step_s": 2.0}
    mins = f"{secs / 60:g}min"
    run = f"{sc['name']}_{mins}_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir = os.path.join(ROOT, "runs", run)
    os.makedirs(run_dir)
    json.dump(sc, open(os.path.join(run_dir, "scenario.json"), "w"), indent=1)
    print(f"[sender] {'PAYLOAD profile (about ' + c.rate(round(a.rate)) + ')' if payload else c.rate(a.rate)} for {mins} -> runs/{run}", flush=True)
    from . import selection
    sel = selection.Selector(); sel.refresh()
    if sel.disabled:
        print(f"[sender] NOTE: {len(sel.disabled)} item(s) are switched off in the dashboard selection list ({', '.join(sorted(sel.disabled))[:150]}). Tick them again in the dashboard to send everything.", flush=True)
    rc = subprocess.run([sys.executable, "-m", "traffic.pp", "--scenario", os.path.join(run_dir, "scenario.json"), "--role", "A", "--run-dir", run_dir,
                         "--duration", str(secs), "--start-in", "8", "--announce"], cwd=ROOT, env={**os.environ, "PYTHONPATH": ROOT}).returncode
    try:
        s = json.load(open(os.path.join(run_dir, "stats_A.json")))
        sent = sum(v["msgs"] for v in s["tx"].values()); got = sum(v["msgs"] for v in s["rx"].values()); gap = sum(v["lost"] for v in s["rx"].values())
        if sc.get("echo") is False:
            print(f"\n[sender] SENDER VIEW: sent {sent} messages and {s['filler']['tx_pkts']} stream packets (one way, no echo). The receiver PC builds the summary.", flush=True)
        else:
            print(f"\n[sender] SENDER VIEW: sent {sent} messages, {got} echoes came back, {sent - got} not echoed (round trip), {gap} sequence gaps on the way back, "
                  f"{s['filler']['tx_pkts']} ONF packets sent / {s['filler']['rx_pkts']} received. Full summary is built on the receiver PC.", flush=True)
    except (OSError, ValueError, KeyError):
        pass
    if os.environ.get("SUDO_UID"):     # run as root inside the namespace: hand the files back to you
        subprocess.run(["chown", "-R", f"{os.environ['SUDO_UID']}:{os.environ.get('SUDO_GID', os.environ['SUDO_UID'])}", run_dir])
    sys.exit(rc)


if __name__ == "__main__":
    main()
