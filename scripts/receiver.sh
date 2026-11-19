#!/usr/bin/env bash
# RECEIVER + ECHO side. Works on either PC. Same arguments as the sender, so you type the same thing on both PCs:
#     scripts/receiver.sh 75 10        # wait for ONE run of 75 Mbit/s, 10 minutes (refuses a sender that chose something else), then exit
#     scripts/receiver.sh              # no arguments: follow whatever the sender chooses, run after run, until Ctrl-C
# This PC needs: the GE1 PC -> sudo scripts/netns_setup.sh rxonly; the Aspire -> its test NIC at 192.168.50.2 (sudo scripts/nic_role.sh receiver)
# After each run it fetches the sender's results over ssh and writes the summary + Excel.
# Dashboard: http://localhost:3000/d/pingpong-450-receiver  (pick this PC's switch port in the "Switch port" box at the top)
source "$(dirname "$0")/_pp_env.sh"
RATE=${1:-}; MIN=${2:-}
ARGS=(); [ -n "$RATE" ] && ARGS+=(--rate "$RATE"); [ -n "$MIN" ] && ARGS+=(--minutes "$MIN")
if pp_has_ns rx; then :; else
  ip -o -4 addr | grep -q "192.168.50.2/" || { echo "This PC is not set up as the receiver (no 192.168.50.2). On the GE1 PC: sudo scripts/netns_setup.sh rxonly   On the Aspire: sudo scripts/nic_role.sh receiver"; exit 1; }
fi
scripts/start_stack.sh >/dev/null 2>&1 && echo "Dashboard: http://localhost:3000/d/pingpong-450-receiver   (this PC = $PP_PORT)"
if [ -n "$RATE" ]; then MBS=$(awk -v r="$RATE" 'BEGIN{printf "%.3g", r/8}'); echo "Expecting: $RATE Mbit/s ($MBS MB/s), ${MIN:-any} min"; elif [ -n "$MIN" ]; then echo "Expecting: any load, $MIN min"; fi
while true; do
  rm -f .STOP data/last_run.txt
  pp_exec rx "$PY" -m traffic.receiver "${ARGS[@]}" || { [ -e .STOP ] && break; sleep 2; continue; }
  [ -s data/last_run.txt ] && "$PY" -m traffic.finish "$(cat data/last_run.txt)"
  [ -n "$RATE$MIN" ] && break          # with arguments: one run, then done
  echo; echo "--- ready for the next run ---"
done
