#!/usr/bin/env bash
# RECEIVER + ECHO PC (switch port GE1). Start it once and leave it running. It waits for the sender, runs whatever the
# sender chose (load and length), fetches the sender's results, writes the summary + Excel, then waits for the next run.
#     scripts/receiver.sh
# Dashboard: http://localhost:3000/d/pingpong-450-receiver     Stop: Ctrl-C (or scripts/estop.sh to stop a run in progress)
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
ROOT=$PWD
IP=$(readlink -f "$(command -v ip)")
$IP netns list | grep -qw rx || { echo "no 'rx' namespace: run  sudo scripts/netns_setup.sh rxonly  first"; exit 1; }
scripts/start_stack.sh >/dev/null 2>&1 && echo "Dashboard: http://localhost:3000/d/pingpong-450-receiver"
export PP_TX_HOST=${PP_TX_HOST:-abhijith-m-b@192.168.1.40} PP_TX_DIR=${PP_TX_DIR:-ins450-pingpong-test}
while true; do
  rm -f .STOP data/last_run.txt
  sudo -n "$IP" netns exec rx env PYTHONPATH="$ROOT" "$ROOT/.venv/bin/python" -m traffic.receiver || { [ -e .STOP ] && break; sleep 2; continue; }
  [ -s data/last_run.txt ] && .venv/bin/python -m traffic.finish "$(cat data/last_run.txt)"
  echo; echo "--- ready for the next run ---"
done
