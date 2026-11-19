#!/usr/bin/env bash
# Runs the profiles with the sender on ANOTHER PC (Aspire on GE9, 100 Mbit/s), 10 min each, unattended.
#   scripts/run_all_remote.sh                 -> 25 50 75 95
# Needs: sudo scripts/netns_setup.sh rxonly (once), ssh key to the sender PC. Emergency stop: scripts/estop.sh
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
export PP_TX_HOST=${PP_TX_HOST:-abhijith-m-b@192.168.1.40} PP_TX_DIR=${PP_TX_DIR:-ins450-pingpong-test}
OUT=runs/sender_GE9
mkdir -p "$OUT"
PROFILES=${*:-025 050 075 095}
for p in $PROFILES; do
  p=$(printf '%03d' "$((10#$p))")
  echo "=== profile $p Mbit/s ($(date +%T)) ==="
  .venv/bin/python -m traffic.runner "scenarios/pp9_${p}mbps.json" --runs-dir "$OUT" || break
  [ -e .STOP ] && break
  echo "cool-down 30 s"; sleep 30
done
RUNS_DIR=$OUT .venv/bin/python tools/make_zoho_xlsx.py "$OUT/results_for_zoho.xlsx"
