#!/usr/bin/env bash
# Runs every profile in order (25, 50, 75, 100, 150, 200 Mbit/s per direction), 10 minutes each, with a pause between.
#   sudo scripts/netns_setup.sh            (once, first)
#   scripts/run_all.sh                     (about 70 minutes, no sudo after scripts/enable_sudoless.sh)   or  scripts/run_all.sh 100 150
# Emergency stop: scripts/estop.sh
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
PREFIX=${PREFIX:-pp_}    # PREFIX=pp_unmarked_ scripts/run_all.sh 100 150 200   -> the unmarked variants
PROFILES=${*:-025 050 075 100 150 200}
for p in $PROFILES; do
  p=$(printf '%03d' "$((10#$p))")
  echo "=== profile $p Mbit/s ==="
  .venv/bin/python -m traffic.runner "scenarios/${PREFIX}${p}mbps.json" || break
  [ -e .STOP ] && break
  echo "cool-down 30 s"; sleep 30
done
