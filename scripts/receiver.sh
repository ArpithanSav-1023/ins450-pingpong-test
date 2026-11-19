#!/usr/bin/env bash
# RECEIVER + ECHO PC (switch port GE1). Same arguments as the sender, so you type the same thing on both PCs:
#     scripts/receiver.sh 75 10        # wait for ONE run of 75 Mbit/s, 10 minutes (refuses a sender that chose something else), then exit
#     scripts/receiver.sh              # no arguments: follow whatever the sender chooses, run after run, until Ctrl-C
# After each run it fetches the sender's results, writes the summary + Excel.
# Dashboard: http://localhost:3000/d/pingpong-450-receiver     Stop: Ctrl-C (or scripts/estop.sh to stop a run in progress)
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
ROOT=$PWD
RATE=${1:-}; MIN=${2:-}
ARGS=(); [ -n "$RATE" ] && ARGS+=(--rate "$RATE"); [ -n "$MIN" ] && ARGS+=(--minutes "$MIN")
IP=$(readlink -f "$(command -v ip)")
$IP netns list | grep -qw rx || { echo "no 'rx' namespace: run  sudo scripts/netns_setup.sh rxonly  first"; exit 1; }
scripts/start_stack.sh >/dev/null 2>&1 && echo "Dashboard: http://localhost:3000/d/pingpong-450-receiver"
export PP_TX_HOST=${PP_TX_HOST:-abhijith-m-b@192.168.1.40} PP_TX_DIR=${PP_TX_DIR:-ins450-pingpong-test}
[ -n "$RATE$MIN" ] && echo "Expecting: ${RATE:-any} Mbit/s, ${MIN:-any} min"
while true; do
  rm -f .STOP data/last_run.txt
  sudo -n "$IP" netns exec rx env PYTHONPATH="$ROOT" "$ROOT/.venv/bin/python" -m traffic.receiver "${ARGS[@]}" || { [ -e .STOP ] && break; sleep 2; continue; }
  [ -s data/last_run.txt ] && .venv/bin/python -m traffic.finish "$(cat data/last_run.txt)"
  [ -n "$RATE$MIN" ] && break          # with arguments: one run, then done
  echo; echo "--- ready for the next run ---"
done
