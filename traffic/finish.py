"""After a run on the receiver PC: fetch the sender PC's results (ssh), then write summary.json / summary_groups.csv / Excel.
    .venv/bin/python -m traffic.finish runs/aspire_sender/<run>      (no ssh? copy stats_A.json + feed_A.log into the run folder, then run it again)"""
import json
import os
import sys
import time

from . import common as c
from .runner import pull, print_summary, summarize

HOST = os.environ.get("PP_TX_HOST", "abhijith-m-b@192.168.1.40")
TXDIR = os.environ.get("PP_TX_DIR", "ins450-pingpong-test")


def final(run_dir):
    try:
        return json.load(open(os.path.join(run_dir, "stats_A.json"))).get("final")
    except (OSError, ValueError):
        return False


def main():
    run_dir = os.path.abspath(sys.argv[1])
    for i in range(30):                              # the sender may still be finishing, or Wi-Fi may be down for a moment
        if final(run_dir):
            break
        pull(HOST, f"{TXDIR}/runs/{os.path.basename(run_dir)}", run_dir)
        if final(run_dir):
            break
        if i == 0:
            print(f"[finish] fetching the sender's results from {HOST} ...", flush=True)
        time.sleep(10)
    if not final(run_dir):
        sys.exit(f"[finish] sender results not available. Copy stats_A.json and feed_A.log from {HOST}:{TXDIR}/runs/{os.path.basename(run_dir)}/ into {run_dir}/, then run again.")
    print_summary(summarize(run_dir))
    try:
        from tools import make_excel
        print("[finish] Excel:", make_excel.build(run_dir), "| all profiles:", make_excel.build_all(os.path.dirname(run_dir)))
    except Exception as e:
        print("[finish] Excel not written:", e)


if __name__ == "__main__":
    main()
