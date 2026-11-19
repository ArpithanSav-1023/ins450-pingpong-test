#!/usr/bin/env bash
# Stops Prometheus/Grafana containers (data kept) and the two exporters. `docker compose down -v` would also delete stored history.
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
docker compose down
pkill -f "monitor/pp_exporter.py" ; pkill -f "monitor/poller.py"
echo "stack stopped"
