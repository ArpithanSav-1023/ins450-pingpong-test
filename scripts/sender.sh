#!/usr/bin/env bash
# SENDER PC (the separate PC on switch port GE9). Choose the load and the length, nothing else:
#     scripts/sender.sh 50 10          # 50 Mbit/s for 10 minutes
#     scripts/sender.sh                # asks you
# Starts this PC's own dashboard first (if set up with scripts/setup_sender_pc.sh): http://localhost:3000/d/pingpong-450-sender
# The receiver PC just has to be running scripts/receiver.sh; it follows by itself. Stop early: Ctrl-C.
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
RATE=${1:-}; MIN=${2:-}
[ -n "$RATE" ] || read -rp "Load per direction in Mbit/s (e.g. 25 50 75 95): " RATE
[ -n "$MIN" ] || read -rp "Duration in minutes (e.g. 10): " MIN
ip -o -4 addr | grep -q "192.168.50.1/" || { echo "This PC's test NIC has no 192.168.50.1 (run scripts/setup_sender_pc.sh once, or: sudo nmcli connection up pp-test)"; exit 1; }
if command -v docker >/dev/null 2>&1 && [ -x .venv/bin/python ]; then
  scripts/start_stack.sh >/dev/null 2>&1 && echo "Dashboard: http://localhost:3000/d/pingpong-450-sender"
else
  echo "(dashboard not set up on this PC: run  sudo scripts/setup_sender_pc.sh  once. The test runs without it.)"
fi
rm -f .STOP
PY=python3; [ -x .venv/bin/python ] && PY=.venv/bin/python
exec $PY -m traffic.sender_cli --rate "$RATE" --minutes "$MIN"
