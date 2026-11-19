"""RECEIVER side, runs on the receiver/echo PC inside the 'rx' namespace (scripts/receiver.sh does that).
Waits for the sender's HELLO2 (it carries the scenario, duration and run name), then runs endpoint B for exactly that run and exits.
You never pass a scenario or duration here: you choose them on the SENDER only."""
import argparse
import base64
import json
import os
import socket
import subprocess
import sys
import time
import zlib

from . import common as c
from .pp import CTRL_PORT

ROOT = c.PROJECT_ROOT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", default=None, help="default: runs/sender_<port of the sender>, e.g. runs/sender_GE9")
    ap.add_argument("--ip", default="192.168.50.2")
    ap.add_argument("--rate", type=float, help="expect exactly this load (Mbit/s); a sender that chooses another one is refused")
    ap.add_argument("--minutes", type=float, help="expect exactly this length (minutes)")
    a = ap.parse_args()
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.bind((a.ip, CTRL_PORT))
    sk.settimeout(1.0)
    seen_bad = None
    print(f"[receiver] waiting for the sender on {a.ip}:{CTRL_PORT} (Ctrl-C to quit)", flush=True)
    while True:
        if os.path.exists(c.STOP_FILE):
            sys.exit(0)
        try:
            data, src = sk.recvfrom(8000)
        except socket.timeout:
            continue
        if not data.startswith(b"PPHELLO2 "):
            continue
        t_rx = time.time()
        try:
            _, n, dur, run, blob = data.decode().split(" ", 4)
            sc = json.loads(zlib.decompress(base64.b64decode(blob)))
            n, dur = float(n), float(dur)
        except Exception as e:
            print("[receiver] ignoring a malformed HELLO:", e, flush=True)
            continue
        bad = []
        if a.rate is not None and abs(float(sc.get("target_mbps", -1)) - a.rate) > 1e-9:
            bad.append(f"load {c.rate(float(sc.get('target_mbps')))} (you typed {c.rate(a.rate)})")
        if a.minutes is not None and abs(dur / 60 - a.minutes) > 0.01:
            bad.append(f"length {dur / 60:g} min (you typed {a.minutes:g})")
        if bad:
            if run == seen_bad:
                continue                  # the sender repeats its HELLO 8 times: report once
            seen_bad = run
            print("[receiver] MISMATCH, ignoring this sender: it chose " + " and ".join(bad) + ". Fix one side and start the sender again.", flush=True)
            continue
        break
    sk.close()
    run_dir = os.path.join(a.runs_dir or os.path.join(ROOT, "runs", "sender_" + sc.get("a_port", "GE9")), run)
    os.makedirs(run_dir, exist_ok=True)
    json.dump(sc, open(os.path.join(run_dir, "scenario.json"), "w"), indent=1)
    print(f"[receiver] HELLO from {src[0]}: {sc['name']}, {dur:.0f} s, target {c.rate(float(sc.get('target_mbps', 0)))} -> {run_dir}  (starting in {n:.0f} s)", flush=True)
    rc = subprocess.run([sys.executable, "-m", "traffic.pp", "--scenario", os.path.join(run_dir, "scenario.json"), "--role", "B", "--run-dir", run_dir,
                         "--duration", str(dur), "--start-at", str(t_rx + n - 0.001)], cwd=ROOT, env={**os.environ, "PYTHONPATH": ROOT}).returncode
    try:
        open(os.path.join(ROOT, "data", "last_run.txt"), "w").write(run_dir + "\n")
    except OSError:
        pass
    if os.environ.get("SUDO_UID"):     # we run as root inside the namespace: hand the files back to you
        subprocess.run(["chown", "-R", f"{os.environ['SUDO_UID']}:{os.environ.get('SUDO_GID', os.environ['SUDO_UID'])}", run_dir, os.path.join(ROOT, "data", "last_run.txt")])
    sys.exit(rc)


if __name__ == "__main__":
    main()
