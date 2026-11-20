# Running the test: sender PC + receiver PC

> **Units:** everywhere a rate is shown in Mbit/s, MB/s is given too (8 Mbit/s = 1 MB/s). 25 Mbit/s (3.1 MB/s), 50 (6.25), 75 (9.4), 95 (11.9), 100 (12.5), 150 (18.75), 200 (25 MB/s).


Two PCs, each runs ONE script. You choose the load and the length on the **sender** only.

| PC | Role | Switch port | Script | Dashboard |
|---|---|---|---|---|
| Aspire (separate PC) | **Sender** | GE9 (100 Mbit/s) | `scripts/sender.sh <Mbit/s> <minutes>` | http://localhost:3000/d/pingpong-450-sender |
| This PC | **Receiver + echo** | GE1 | `scripts/receiver.sh` | http://localhost:3000/d/pingpong-450-receiver |

## Every run
1. Receiver PC: `scripts/receiver.sh 50 10` (same two numbers as the sender; it refuses a sender that chose something else, and exits after the run). Without numbers it follows the sender run after run.
2. Sender PC: `scripts/sender.sh 50 10`  (50 Mbit/s per direction, 10 minutes; without arguments it asks)

The receiver gets the scenario, the length and the run name from the sender's start signal (HELLO), so nothing else is typed.
When the run ends the receiver fetches the sender's results over ssh and writes `summary.json`, `summary_groups.csv` and the Excel into
`runs/sender_GE9/<run>/` (plus `all_profiles_summary.xlsx`). If ssh is down it tells you which two files to copy.
Stop early: Ctrl-C on the sender (the receiver stops with it), or `scripts/estop.sh`.

## One-time setup
* Receiver PC: `sudo scripts/netns_setup.sh rxonly` (and `sudo scripts/enable_sudoless.sh` once).
* Sender PC: `git clone <repo> ~/ins450-pingpong-test`, then `sudo scripts/setup_sender_pc.sh` (docker, snmp, venv, permanent 192.168.50.1 on the test NIC, port 514).
  Log out and in again afterwards so docker works without sudo.

## Limits
The sender refuses a load that does not fit its link (100 Mbit/s NIC: about 97 Mbit/s). Maximum run length 60 minutes (`MAX_DURATION_S`).

## Official radar stream (HALO20 spokes)

The sender also replays the official radar stream from a real capture (`/home/sav1023/Code/test/radar_dump.pcapng`) on its own multicast group **239.6.7.8:6678** (captured on 236.6.7.8; moved into 239/8 because the lab only allows that range). Marked CS2. It is about **5.2 Mbit/s (0.65 MB/s)** per direction, counted inside the target rate (the ONF dump gets the rest). The receiver echoes every radar datagram back unchanged, like the other groups.

- Datagrams are 17160 B, so each one travels as ~12 IP fragments. A lost fragment loses the whole datagram.
- Counted as group `RADAR` in the dashboards, `summary_groups.csv` and an extra last row of the Excel sheet. The in-band counter is the spoke sequence number inside the data.
- One-time setup on the **sender** PC only (the receiver does not need the capture; it never goes into git):
  `.venv/bin/python tools/extract_radar.py /home/sav1023/Code/test/radar_dump.pcapng`  (makes `data/radar_bundles.bin`, 35 MB, 56 s that loops).
  If the sender role moves to the other PC, copy it: `rsync data/radar_bundles.bin <user>@<other pc>:<repo>/data/`.
- Leave it out with `python3 -m traffic.sender_cli --rate 50 --minutes 10 --no-radar`.
- Both PCs need the updated `traffic/pp.py` (the receiver must echo radar).

## Choosing what the sender sends (selection list in Grafana)

The SENDER dashboard has a block **"Choose what the sender sends"** at the bottom: every sensor and sentence type of the mix (e.g. `NAVD  SI9001  GPGGA`) grouped by multicast group, plus the radar stream. Untick what you do not want; tick a group header to switch the whole group. Search box filters the list.

- A change reaches a **running** test within about a second and is remembered for later runs (`data/selection.json`). `sender.sh` prints a NOTE at the start if anything is switched off.
- Left-out sentences are not counted as lost (the sequence counters are only stamped on what is really sent).
- The list is a small page served by `tools/selector.py` on `localhost:9104` (started by `scripts/start_stack.sh`) and embedded in Grafana, so the browser must be on the sender PC. Grafana needs `GF_PANELS_DISABLE_SANITIZE_HTML=true` for that (already set in `docker-compose.yml`).
- The load drops by what you switch off; the ONF dump does not compensate.
