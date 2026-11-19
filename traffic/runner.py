"""Runs one ping-pong scenario: endpoint B (behind GE9) and endpoint A (behind GE1), then writes the summary.

    sudo .venv/bin/python -m traffic.runner scenarios/pp_050mbps.json          # real run through the switch (needs scripts/netns_setup.sh)
    .venv/bin/python -m traffic.runner scenarios/pp_050mbps.json --local --duration 20   # loopback self-test, no root
Emergency stop: scripts/estop.sh (or Ctrl-C).
"""
import argparse
import csv
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time

from . import common as c

ROOT = c.PROJECT_ROOT
IP = os.path.realpath(shutil.which("ip") or "/usr/bin/ip")   # absolute path: it must match the sudoers rule


SSH_KEY = os.environ.get("PP_SSH_KEY", os.path.expanduser("~/.ssh/id_pp_sender"))
SSH_OPTS = ["-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/pp-ssh-%C", "-o", "ControlPersist=120"]


def ssh(host, cmd, **kw):
    return subprocess.run(["ssh", *SSH_OPTS, host, cmd], capture_output=True, text=True, **kw)


def pull(host, remote_run, run_dir):
    """Copies the remote endpoint's stats/feed files into the local run dir (rsync over the same ssh connection)."""
    subprocess.run(["rsync", "-a", "-e", "ssh " + " ".join(SSH_OPTS), f"{host}:{remote_run}/", run_dir + "/"], capture_output=True)


def summarize(run_dir):
    A = json.load(open(os.path.join(run_dir, "stats_A.json")))
    B = json.load(open(os.path.join(run_dir, "stats_B.json")))
    dirs = {"GE1->GE9": (A, B), "GE9->GE1": (B, A)}   # (sender, receiver)
    out = {"scenario": A["scenario"], "target_mbps": A["target_mbps"], "directions": {}}
    rows = []
    for d, (s, r) in dirs.items():
        tot = dict(sent=0, recv=0, lost=0, gap_lost=0, bad=0, wrong_dscp=0, sent_bytes=0, recv_bytes=0)
        for g, t in s["tx"].items():
            rx = r["rx"][g]
            lost = max(t["msgs"] - rx["msgs"], 0)          # exact: sender count minus receiver count
            rows.append([d, g, t["msgs"], rx["msgs"], lost, rx["lost"], rx["bad"], rx["wrong_dscp"], t["bytes"], rx["bytes"]])
            for k, v in zip(tot, (t["msgs"], rx["msgs"], lost, rx["lost"], rx["bad"], rx["wrong_dscp"], t["bytes"], rx["bytes"])):
                tot[k] += v
        f_s, f_r = s["filler"], r["filler"]
        f_lost = max(f_s["tx_pkts"] - f_r["rx_pkts"], 0)
        pk_sent, pk_recv = tot["sent"] + f_s["tx_pkts"], tot["recv"] + f_r["rx_pkts"]
        by_sent, by_recv = tot["sent_bytes"] + f_s["tx_bytes"], tot["recv_bytes"] + f_r["rx_bytes"]
        dur = max(s.get("sent_secs") or s.get("duration_s") or 1, 1)
        out["directions"][d] = {
            "messages": {**tot, "loss_pct": 100 * tot["lost"] / tot["sent"] if tot["sent"] else 0},
            "filler": {"sent_pkts": f_s["tx_pkts"], "recv_pkts": f_r["rx_pkts"], "lost_pkts": f_lost, "achieved_mbps": round(f_s["tx_pkts"] * 1446 * 8 / 1e6 / dur, 2)},
            "packets": {"sent": pk_sent, "recv": pk_recv, "lost": max(pk_sent - pk_recv, 0), "loss_pct": 100 * max(pk_sent - pk_recv, 0) / pk_sent if pk_sent else 0},
            "data": {"sent_bytes": by_sent, "recv_bytes": by_recv, "lost_bytes": max(by_sent - by_recv, 0), "loss_pct": 100 * max(by_sent - by_recv, 0) / by_sent if by_sent else 0}}
    json.dump(out, open(os.path.join(run_dir, "summary.json"), "w"), indent=1)
    with open(os.path.join(run_dir, "summary_groups.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["direction", "group", "sent", "received", "lost_exact", "lost_gap_count", "bad", "wrong_dscp", "sent_bytes", "recv_bytes"])
        w.writerows(rows)
    return out


def print_summary(o):
    print(f"\n=== {o['scenario']} (target {o['target_mbps']} Mbit/s per direction) ===")
    for d, v in o["directions"].items():
        m, p, b = v["messages"], v["packets"], v["data"]
        print(f"{d}:  messages {m['sent']} sent / {m['recv']} recv / {m['lost']} lost ({m['loss_pct']:.3f}%), bad {m['bad']}, wrong DSCP {m['wrong_dscp']}\n"
              f"          packets  {p['sent']} sent / {p['recv']} recv / {p['lost']} lost ({p['loss_pct']:.3f}%)   ONF dump achieved (wire) {v['filler']['achieved_mbps']} Mbit/s\n"
              f"          data     {b['sent_bytes']} B sent / {b['recv_bytes']} B recv / {b['lost_bytes']} B lost ({b['loss_pct']:.3f}%)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--duration", type=float)
    ap.add_argument("--label", default="")
    ap.add_argument("--tx-host", default=os.environ.get("PP_TX_HOST"), help="user@host of a separate sender PC (endpoint A runs there over ssh; see scripts/sync_sender.sh)")
    ap.add_argument("--tx-dir", default=os.environ.get("PP_TX_DIR", "pp"), help="project folder on the sender PC (relative to its home)")
    ap.add_argument("--manual-sender", action="store_true", help="sender PC is started by hand: print its command, start only B here, then wait for stats_A.json to be copied back")
    ap.add_argument("--lead", type=float, default=120.0, help="--manual-sender: seconds from now until both ends start sending")
    ap.add_argument("--runs-dir", default=os.path.join(ROOT, "runs"))
    a = ap.parse_args()
    sc = json.load(open(a.scenario))
    if not a.local and os.geteuid() != 0:   # not root: use passwordless sudo for `ip netns exec` only (see scripts/enable_sudoless.sh)
        if subprocess.run(["sudo", "-n", IP, "netns", "list"], capture_output=True).returncode != 0:
            sys.exit("cannot run `sudo -n ip netns ...`: run  sudo scripts/enable_sudoless.sh  once (or run this command with sudo)")
    if sc.get("target_mbps", 0) > 250:
        sys.exit("REFUSING: target above 250 Mbit/s")
    dur = min(float(a.duration or sc["duration_s"]), c.MAX_DURATION_S)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(a.runs_dir, f"{sc['name']}{('_' + a.label) if a.label else ''}_{stamp}")
    os.makedirs(run_dir)
    shutil.copy(a.scenario, os.path.join(run_dir, "scenario.json"))
    if os.path.exists(c.STOP_FILE):
        os.remove(c.STOP_FILE)
    try:
        open(os.path.join(ROOT, "data", "phase.txt"), "w").write(sc["name"] + "\n")
    except OSError:
        pass

    remote = a.tx_host if not a.local else None
    remote_run = f"{a.tx_dir}/runs/{os.path.basename(run_dir)}"
    offset = 0.0
    if remote:   # sender is another PC: check ssh, copy the scenario, measure its clock offset
        if ssh(remote, "true").returncode != 0:
            sys.exit(f"cannot ssh to {remote} without a password (run: ssh-copy-id {remote})")
        r = ssh(remote, f"test -d {a.tx_dir}/traffic && mkdir -p {remote_run} && rm -f {a.tx_dir}/.STOP")
        if r.returncode != 0:
            sys.exit(f"remote project folder missing ({a.tx_dir}): run scripts/sync_sender.sh {remote}")
        subprocess.run(["scp", *SSH_OPTS, os.path.join(run_dir, "scenario.json"), f"{remote}:{remote_run}/scenario.json"], capture_output=True, check=True)
        offs = []
        for _ in range(5):
            t0 = time.time(); rt = ssh(remote, "date +%s.%N").stdout.strip(); t1 = time.time()
            offs.append((float(rt) - (t0 + t1) / 2, t1 - t0))
        offset = min(offs, key=lambda x: x[1])[0]     # sample with the shortest round trip is the most accurate
        print(f"[runner] sender PC {remote}: clock offset {offset * 1000:+.1f} ms (compensated)", flush=True)
        if abs(offset) > 1.0:
            print("[runner] WARNING: clocks differ by more than 1 s: install chrony on both PCs so Prometheus windows line up", flush=True)

    start_at = time.time() + (a.lead if a.manual_sender else 6.0 if remote else 4.0)      # receivers (joins, ARP, IGMP) settle before anyone sends

    def launch(role, ns):
        if remote and role == "A":
            rc = f"cd {a.tx_dir} && PYTHONPATH=. python3 -m traffic.pp --scenario {remote_run}/scenario.json --role A --run-dir {remote_run} --duration {dur} --start-at {start_at + offset}"
            return subprocess.Popen(["ssh", *SSH_OPTS, remote, rc])
        cmd = [sys.executable, "-m", "traffic.pp", "--scenario", os.path.join(run_dir, "scenario.json"), "--role", role, "--run-dir", run_dir, "--duration", str(dur), "--start-at", str(start_at)]
        if a.local:
            cmd.append("--local")
        else:
            cmd = (["sudo", "-n"] if os.geteuid() != 0 else []) + [IP, "netns", "exec", ns, "env", f"PYTHONPATH={ROOT}"] + cmd
        return subprocess.Popen(cmd, cwd=ROOT, env={**os.environ, "PYTHONPATH": ROOT})

    pb = launch("B", sc.get("rx_netns", "rx"))
    time.sleep(0.5)
    if a.manual_sender:
        name = os.path.basename(run_dir)
        print(f"\n[runner] MANUAL SENDER. Paste this on the sender PC within {a.lead:.0f} s (both ends start sending at the same clock time, {time.strftime('%H:%M:%S', time.localtime(start_at))}):\n\n"
              f"  cd ~/pp && mkdir -p runs/{name} && python3 -m traffic.pp --scenario scenarios/{os.path.basename(a.scenario)} --role A --run-dir runs/{name} --duration {dur} --start-at {start_at}\n", flush=True)
        pa = None
    else:
        pa = launch("A", sc.get("tx_netns", "tx"))
    print(f"[runner] {sc['name']}: {dur:.0f}s (+5 s drain) -> {run_dir}", flush=True)
    sync_stop = threading.Event()

    def syncer():     # live view for Grafana: copy A's files here every 2 s; forward a STOP to the remote sender
        stop_sent = False
        while not sync_stop.wait(2.0):
            if os.path.exists(c.STOP_FILE) and not stop_sent:
                ssh(remote, f"touch {a.tx_dir}/.STOP"); stop_sent = True
            pull(remote, remote_run, run_dir)
    if remote:
        threading.Thread(target=syncer, daemon=True).start()
    def wait_manual_stats():
        f = os.path.join(run_dir, "stats_A.json")
        print(f"[runner] receiver done. Copy the sender's results here (from the sender PC):\n\n"
              f"  rsync -a ~/pp/runs/{os.path.basename(run_dir)}/ {os.environ.get('USER', 'user')}@<this-PC-ip>:{run_dir}/\n"
              f"  (or copy ~/pp/runs/{os.path.basename(run_dir)}/stats_A.json and feed_A.log into {run_dir}/ by USB stick)\n", flush=True)
        while not os.path.exists(os.path.join(c.PROJECT_ROOT, ".STOP")):
            try:
                if json.load(open(f)).get("final"):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(2)
    try:
        if pa:
            pa.wait()
        pb.wait()
        if a.manual_sender:
            wait_manual_stats()
    except KeyboardInterrupt:
        open(c.STOP_FILE, "w").close()
        if remote:
            ssh(remote, f"touch {a.tx_dir}/.STOP")
        if pa:
            pa.wait()
        pb.wait()
    if remote:
        sync_stop.set()
        time.sleep(0.5)
        pull(remote, remote_run, run_dir)
    stopped = os.path.exists(c.STOP_FILE)
    if stopped:
        os.remove(c.STOP_FILE)
    print_summary(summarize(run_dir))
    try:
        sys.path.insert(0, ROOT)
        from tools import make_excel
        print("[runner] Excel:", make_excel.build(run_dir), "| all profiles:", make_excel.build_all(a.runs_dir))
    except Exception as e:   # the Excel is a convenience: never lose a run over it
        print("[runner] Excel not written:", e)
    if os.environ.get("SUDO_UID"):
        subprocess.run(["chown", "-R", f"{os.environ['SUDO_UID']}:{os.environ.get('SUDO_GID', os.environ['SUDO_UID'])}", run_dir])
    if stopped:
        sys.exit(130)   # run_all.sh stops the whole sequence on this


if __name__ == "__main__":
    main()
