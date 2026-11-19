# Running the test: sender PC + receiver PC

Two PCs, each runs ONE script. You choose the load and the length on the **sender** only.

| PC | Role | Switch port | Script | Dashboard |
|---|---|---|---|---|
| Aspire (separate PC) | **Sender** | GE9 (100 Mbit/s) | `scripts/sender.sh <Mbit/s> <minutes>` | http://localhost:3000/d/pingpong-450-sender |
| This PC | **Receiver + echo** | GE1 | `scripts/receiver.sh` | http://localhost:3000/d/pingpong-450-receiver |

## Every run
1. Receiver PC (once, leave it running): `scripts/receiver.sh`
2. Sender PC: `scripts/sender.sh 50 10`  (50 Mbit/s per direction, 10 minutes; without arguments it asks)

The receiver gets the scenario, the length and the run name from the sender's start signal (HELLO), so nothing else is typed.
When the run ends the receiver fetches the sender's results over ssh and writes `summary.json`, `summary_groups.csv` and the Excel into
`runs/aspire_sender/<run>/` (plus `all_profiles_summary.xlsx`). If ssh is down it tells you which two files to copy.
Stop early: Ctrl-C on the sender (the receiver stops with it), or `scripts/estop.sh`.

## One-time setup
* Receiver PC: `sudo scripts/netns_setup.sh rxonly` (and `sudo scripts/enable_sudoless.sh` once).
* Sender PC: `git clone <repo> ~/ins450-pingpong-test`, then `sudo scripts/setup_sender_pc.sh` (docker, snmp, venv, permanent 192.168.50.1 on the test NIC, port 514).
  Log out and in again afterwards so docker works without sudo.

## Limits
The sender refuses a load that does not fit its link (100 Mbit/s NIC: about 97 Mbit/s). Maximum run length 60 minutes (`MAX_DURATION_S`).
