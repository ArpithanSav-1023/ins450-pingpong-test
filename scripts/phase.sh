#!/usr/bin/env bash
# Label the current experiment phase; it shows on the Grafana dashboard (e.g.  scripts/phase.sh "priority OFF").
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
mkdir -p data; echo "${*:-unlabeled}" > data/phase.txt; echo "phase: $(cat data/phase.txt)"
