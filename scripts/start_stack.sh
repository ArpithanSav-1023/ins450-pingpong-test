#!/usr/bin/env bash
# Starts the monitoring stack: SNMP poller (:9101), ping-pong exporter (:9103), Prometheus (:9090), Grafana (:3000, localhost only).
# Safe to run repeatedly (skips what is already running). Stop with scripts/stop_stack.sh.
cd "$(cd "$(dirname "$0")/.." && pwd)" || exit 1
mkdir -p data
up() { ss -ltn 2>/dev/null | grep -q ":$1 "; }
if up 9101; then echo "poller already running (:9101)"; else
  nohup .venv/bin/python monitor/poller.py --interval 5 --db data/exp_a.sqlite >> data/poller.log 2>&1 & echo "poller started"; fi
if up 9103; then echo "pingpong exporter already running (:9103)"; else
  nohup .venv/bin/python monitor/pp_exporter.py >> data/pp_exporter.log 2>&1 & echo "pingpong exporter started"; fi
if up 9104; then echo "selection list already running (:9104)"; else
  nohup .venv/bin/python tools/selector.py >> data/selector.log 2>&1 & echo "selection list started (:9104)"; fi
[ -f data/phase.txt ] || echo "unlabeled" > data/phase.txt
docker compose up -d
echo; echo "Grafana:    http://localhost:3000   (dashboard opens by default)"
echo "Prometheus: http://localhost:9090"
echo "Phone access: see docs/HOW_TO_RUN.md (tailscale serve)"
