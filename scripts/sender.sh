#!/usr/bin/env bash
# SENDER side. Works on either PC. Choose the load and the length, nothing else:
#     scripts/sender.sh 50 10          # 50 Mbit/s for 10 minutes
#     scripts/sender.sh                # asks you
# Start scripts/receiver.sh with the SAME two numbers on the other PC first.
# This PC needs: the Aspire -> its test NIC at 192.168.50.1 (sudo scripts/nic_role.sh sender); the GE1 PC -> sudo scripts/netns_setup.sh txonly
# Dashboard: http://localhost:3000/d/pingpong-450-sender  (pick this PC's switch port in the "Switch port" box at the top)
source "$(dirname "$0")/_pp_env.sh"
RATE=${1:-}; MIN=${2:-}
[ -n "$RATE" ] || read -rp "Load per direction in Mbit/s (e.g. 25 50 75 95): " RATE
[ -n "$MIN" ] || read -rp "Duration in minutes (e.g. 10): " MIN
if pp_has_ns tx; then :; else
  ip -o -4 addr | grep -q "192.168.50.1/" || { echo "This PC is not set up as the sender (no 192.168.50.1). On the GE1 PC: sudo scripts/netns_setup.sh txonly   On the Aspire: sudo scripts/nic_role.sh sender"; exit 1; }
fi
if command -v docker >/dev/null 2>&1 && [ -x .venv/bin/python ]; then
  scripts/start_stack.sh >/dev/null 2>&1 && echo "Dashboard: http://localhost:3000/d/pingpong-450-sender   (this PC = $PP_PORT)"
else
  echo "(dashboard not set up on this PC: run  sudo scripts/setup_sender_pc.sh  once. The test runs without it.)"
fi
rm -f .STOP
pp_exec tx "$PY" -m traffic.sender_cli --rate "$RATE" --minutes "$MIN"
