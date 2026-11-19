# IEC 61162-450 ping-pong test (CBS350, GE1 <-> GE9)

GE1 host sends the 450 sample mix on every multicast group, the GE9 host echoes every datagram back on the same group, repeat for 10 minutes.
Chart data is added as paced UDP filler in both directions so each run offers 25 / 50 / 75 / 100 / 150 / 200 Mbit/s per direction.
Grafana shows port utilisation, message counters (each side), packet / message / data loss and lost-message counts.

## Layout
- `samples/`            your files: `mix.xlsx` (group list), `ins450/` (sample payloads + test_sender.py), `charts/` (S-57 filler)
- `tools/gen_scenarios.py`  mix.xlsx -> `scenarios/pp_*.json`  (30 groups, 38 messages per round)
- `traffic/pp.py`       the endpoint (role A behind GE1, role B behind GE9); `traffic/runner.py` runs a scenario and writes `runs/<run>/summary.json`
- `monitor/poller.py`   SNMP port counters :9101;  `monitor/pp_exporter.py` loss/counters :9103
- `config/`, `docker-compose.yml`  Prometheus + Grafana;  dashboard `config/grafana/dashboards/pingpong450.json` (rebuild: `tools/make_dashboard.py`)

## Run
    .venv/bin/python tools/gen_scenarios.py                              # only if the sheet changed
    .venv/bin/python -m traffic.runner scenarios/pp_050mbps.json --local --duration 20    # self-test, no root, no network
    sudo scripts/netns_setup.sh                                          # tx ns -> GE1, rx ns -> GE9
    scripts/start_stack.sh                                               # poller, exporter, Prometheus, Grafana (http://localhost:3000)
    scripts/run_all.sh                                                   # all six profiles, ~70 min (after: sudo scripts/enable_sudoless.sh, once)
    sudo scripts/netns_teardown.sh ; scripts/stop_stack.sh               # afterwards
Stop a run at any time: `scripts/estop.sh`.

## Notes
- Offered rate = wire rate (Ethernet+FCS+IP+UDP) per direction, mix + filler. If GE9 is a 100 Mbit/s NIC, profiles >= 100 saturate it and the loss is link saturation.
- Loss is from in-band counters (NMEA tag-block n:, binary block-id, syslog PROCID, filler seq), so it is exact; summary.json also uses sent-minus-received so the last packets of a run count.
- DSCP follows test_sender.py (CS5 NAVD/SATD/TGTD ... CS1 PROP/USR); filler is CS1. "wrong DSCP" counts packets that arrive with a different class.

## Excel output
Every run writes `runs/<run>/results_<scenario>.xlsx` (same layout as `samples/mix.xlsx`: Received / Bad / Lost (n: gaps) / Wrong DSCP per group, Missing and Result by the sheet's own formulas)
with sheets `Groups and samples` (GE1 -> GE9), `Echo GE9 to GE1` (reply path), `Loss summary` (message / packet / data loss, filler rate, switch port utilisation).
`runs/all_profiles_summary.xlsx` has one row per run and direction. Rebuild any time: `.venv/bin/python tools/make_excel.py runs/<run>` or `--all`.

## Running the sender on a second PC (Python 3 only, no root, no packages)
    git clone <repo> ~/pp && cd ~/pp
    # sender NIC: sudo ip addr add 192.168.50.1/24 dev <NIC>; sudo sysctl -w net.ipv4.conf.all.rp_filter=0
    # the receiver PC prints the exact command to paste here (.venv/bin/python -m traffic.runner <scenario> --manual-sender)
    # results are in ~/pp/runs/<run>/ : copy back by `git`-branch push (see scripts/push_results.sh) or rsync
