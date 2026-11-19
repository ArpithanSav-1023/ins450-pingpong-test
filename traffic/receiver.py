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
    ap.add_argument("--runs-dir", default=os.path.join(ROOT, "runs", "aspire_sender"))
    ap.add_argument("--ip", default="192.168.50.2")
    a = ap.parse_args()
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sk.bind((a.ip, CTRL_PORT))
    sk.settimeout(1.0)
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
        break
    sk.close()
    run_dir = os.path.join(a.runs_dir, run)
    os.makedirs(run_dir, exist_ok=True)
    json.dump(sc, open(os.path.join(run_dir, "scenario.json"), "w"), indent=1)
    print(f"[receiver] HELLO from {src[0]}: {sc['name']}, {dur:.0f} s, target {sc.get('target_mbps')} Mbit/s -> {run_dir}  (starting in {n:.0f} s)", flush=True)
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
