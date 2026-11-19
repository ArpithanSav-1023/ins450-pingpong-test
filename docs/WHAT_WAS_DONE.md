# What was done: the IEC 61162-450 ping-pong test and the switch QoS setup

> **Units:** everywhere a rate is shown in Mbit/s, MB/s is given too (8 Mbit/s = 1 MB/s). 25 Mbit/s (3.1 MB/s), 50 (6.25), 75 (9.4), 95 (11.9), 100 (12.5), 150 (18.75), 200 (25 MB/s).


A beginner-friendly write-up of everything built and changed, in plain language.
Project folder: `/home/sav1023/Code/net/test`  ·  Written 2026-11-19

---

## 1. The big picture (read this first)

**Goal.** On a ship, many devices (GPS, gyro, radar, alarms...) send data over one network. IEC 61162-450 is the standard that says *how* they send it: small text messages (NMEA sentences), sent as UDP **multicast** to fixed addresses called *groups* (for example the navigation group NAVD is `239.192.0.4`, port `60004`).

You wanted to know: **when the network gets busy, do any messages get lost, and how busy are the switch ports?**

**The lab.** One PC, one Cisco CBS350 switch, two cables:

```
   PC "GE1 side" (namespace tx) ---- port GE1 ===[ CBS350 switch ]=== port GE9 ---- PC "GE9 side" (namespace rx)
        (pretends to be the multiplexer)                                  (pretends to be the INS gateway)
```

Both "sides" are on the same PC, but each lives in its own network sandbox (a *namespace*) so the traffic really leaves the PC, crosses the switch, and comes back in.

**The test ("ping-pong").**
1. The GE1 side sends your sample messages on all 30 groups.
2. The GE9 side receives them and **sends one reply back** for every message it got.
3. The GE1 side receives the replies. Repeat for 10 minutes.
4. On top of that, a lot of extra data (the **ONF dump**, made from your real chart files) is sent in both directions so the link carries 25, 50, 75, 100, 150 or 200 Mbit/s.

**What is measured and shown in Grafana + Excel:**
- Port utilisation of GE1 and GE9 (from the switch itself, via SNMP)
- Message counters: sent and received on each side
- **Message loss**, **packet loss** and **data loss** (explained in section 4)
- Which groups lost messages

---

## 2. Words you will see (glossary)

| Word | Meaning |
|---|---|
| **Multicast group** | A special address (`239.192.0.x`) that many devices can listen to at once. |
| **Message** | One 450 sample: one NMEA sentence, one binary block, or one syslog line. |
| **Packet** | Any UDP datagram on the wire (messages **and** the ONF dump). |
| **ONF dump** | Non-450 bulk data (here: chart files) sent to UDP port 20100. It carries most of the load. |
| **DSCP / CoS** | A priority label stamped inside every packet. Higher = more important. |
| **Queue** | The switch has 8 waiting lines per port. Queue 8 is served first, queue 1 last. |
| **Trust DSCP** | The switch reads the DSCP label and uses it to pick a queue. |
| **ACL** | A list of rules that match packets (for example "UDP port 60004"). |
| **Class map / policy** | "If a packet matches this ACL, do this action." A policy bundles several classes. |
| **SNMP** | A way to ask the switch questions, such as "how many bytes has port 9 sent?". |
| **Prometheus / Grafana** | Prometheus stores numbers over time; Grafana draws the charts. |
| **Loki** | Stores text log lines, used for the live message feed. |
| **Namespace** | A private network sandbox inside the PC. |

---

## 3. What I built (the software)

Everything is in `/home/sav1023/Code/net/test`. I started from your earlier `snmp_demo` project and copied the useful parts.

| File / folder | What it does |
|---|---|
| `samples/` | Your inputs: `mix.xlsx` (the group list), `ins450/` (sample payloads and `test_sender.py`), `charts/` (chart files used as ONF data). |
| `tools/gen_scenarios.py` | Reads `mix.xlsx` and writes the test definitions in `scenarios/`. |
| `scenarios/pp_025mbps.json` ... `pp_200mbps.json` | The six marked-traffic tests (25, 50, 75, 100, 150, 200 Mbit/s per direction). |
| `scenarios/pp_unmarked_*mbps.json` | The same tests, but **every packet is sent unmarked** (DSCP 0), like a multiplexer that cannot set priority. |
| `traffic/pp.py` | The test program. One copy runs behind GE1 (role A), one behind GE9 (role B). Sends, receives, echoes, counts. |
| `traffic/sender450.py` | Your `test_sender.py`, unchanged: it builds the real message formats and says which priority each group gets. |
| `traffic/runner.py` | Starts both sides, waits, then writes the summary and the Excel file. |
| `monitor/poller.py` | Asks the switch for port counters every 5 s (SNMP). |
| `monitor/pp_exporter.py` | Turns the test's counters into numbers Prometheus can store. |
| `config/` + `docker-compose.yml` | Starts Prometheus, Grafana, Loki and promtail in containers. |
| `tools/make_dashboard.py` | Generates the Grafana dashboard (36 panels). |
| `tools/make_excel.py` | Builds the Excel results in the same layout as your `mix.xlsx`. |
| `scripts/run_all.sh` | Runs all six profiles one after another (about 70 minutes). |
| `scripts/enable_sudoless.sh` | One-time: lets your user run the test without typing the sudo password. |
| `scripts/estop.sh` | Emergency stop. |

### How loss is counted (exactly, not guessed)

Every message carries a running counter, so the receiver can see a gap:

- **NMEA sentences:** the tag-block `n:` number (1..999, wraps around).
- **Binary messages:** the block-id field in the header.
- **Syslog:** the process-id field.
- **ONF dump packets:** an 8-byte sequence number.

If the receiver sees `n:5` then `n:8`, it knows 2 messages were lost. At the end, the totals also use *sent minus received*, so messages lost right at the end are counted too.

### The 13-sentences-per-second rule

You told me one sensor may send at most **13 sentences per second**. The busiest sensor in the mix (`IN0002`) sends 3 sentences per round, so the mix runs at **4 rounds per second** (12 per second, under the limit). The program refuses to start if any sensor would exceed 13 per second. That is why the mix is small (about 150 messages/s) and the **ONF dump carries almost all of the load**.

---

## 4. The three kinds of loss

| Kind | Counts | Example |
|---|---|---|
| **Message loss** | Lost 450 samples only | 3 of 91,200 NAVD/SATD/... messages lost |
| **Packet loss** | Every lost UDP packet, messages **and** ONF dump | dominated by the ONF dump |
| **Data loss** | Lost payload **bytes** | similar to packet loss, in bytes |

---

## 5. What each tool shows you

### Grafana: `http://localhost:3000/d/pingpong-450`

1. **Header:** test running?, which profile, target rate, switch polling OK?
2. **Port utilisation:** GE1 in/out and GE9 in/out (gauges, graph, Mbit/s).
3. **Message counters:** GE1 sent / GE9 received / GE9 sent (echo) / GE1 received, as full numbers.
4. **Live message feed:** the messages as they are sent and received (sampled: each group about once per 2 s per side). Every lost, bad or wrong-priority message is always shown.
5. **Loss:** lost messages, packets, bytes; loss percentages; lost messages per group.
6. **Per-group table:** sent, received and lost for each multicast group.

### Excel (after each run)

`runs/<run>/results_<scenario>.xlsx`, in the layout of your `mix.xlsx`:
- **Groups and samples:** GE1 → GE9. Received / Bad / Lost (n: gaps) / Wrong DSCP per group. The sheet's own formulas give Missing and Pass/Fail.
- **Echo GE9 to GE1:** the same for the reply path.
- **Loss summary:** message, packet and data loss, achieved ONF rate, switch port utilisation.

`runs/all_profiles_summary.xlsx` has one row per run and direction so you can compare rates.

---

## 6. Results so far

Two complete 10-minute runs, both directions:

| Profile | Messages sent | Messages lost | Packets lost | ONF dump achieved |
|---|---|---|---|---|
| 25 Mbit/s | 91,200 | 0 | 0 | 24.86 Mbit/s |
| 50 Mbit/s | 91,200 | 0 | 0 | 49.86 Mbit/s |

The 75 Mbit/s run was stopped a few seconds after it started. **The 75, 100, 150 and 200 Mbit/s runs have not been done.** Keep in mind that GE9 is a **100 Mbit/s** link (GE1 is 1 Gbit/s), so 100 and above will saturate GE9; loss there is link saturation, not a fault.

---

## 7. Why the switch needed configuring (the QoS part)

The switch only sees packets. To protect important traffic when the link is busy, it needs a **priority label** per packet.

- **In a real system the sending device sets the label** (DSCP), following IEC 61162-460. Our test sender does this using the table in `traffic/sender450.py`.
- **Your multiplexer cannot set it.** Then everything arrives as DSCP 0, all in queue 2, and navigation data is no better off than anything else.
- **So the switch must set the label itself**, at the port where the multiplexer plugs in. That is what I configured, on **GE1's input**.

### Priority table used

| DSCP | Class | Groups | Switch queue (1 = lowest, 8 = highest) |
|---|---|---|---|
| 56 | CS7 | NETA, SYSLOG | 7 |
| 40 | CS5 | TGTD, SATD, NAVD | 7 |
| 32 | CS4 | RCOM | 8 |
| 24 | CS3 | VDRD, TIME, BAM1/2, CAM1/2 | 7 |
| 16 | CS2 | MISC, PGP1-4, PGB1-4 | 7 |
| 8 | CS1 | PROP, USR1-8 | 1 |
| 0 | none | the ONF dump when unmarked | 2 |

All 8 queues use **strict priority** (queue 8 first). Where this is set in the code: `traffic/sender450.py` lines 44-62 (the table), `traffic/pp.py` (applies it to the sockets), `traffic/common.py` (writes it into the packet).

---

## 8. Exactly what I changed on the CBS350 (step by step)

Done through the web page at `http://192.168.1.254/`, while you were logged in.

**Before I changed anything (read only):**
- QoS Mode: Basic. Trust mode: DSCP. Override ingress DSCP: off. Default CoS: 0 on every port.
- IGMP snooping and the IGMP querier were on (VLAN 1). GE1 = 1 Gbit/s, GE9 = 100 Mbit/s.

**Changes:**

| # | Where on the web page | What I set |
|---|---|---|
| 1 | Quality of Service → General → QoS Properties | QoS Mode **Basic → Advanced** (the switch warned that some tables reset to defaults; I confirmed) |
| 2 | QoS → QoS Advanced Mode → Global Settings | Trust Mode **DSCP**; Default Mode Status **Trusted**; Override Ingress DSCP left **off** |
| 3 | Access Control → IPv4-Based ACL | Created 6 ACLs: `CS7_NET`, `CS5_NAV`, `CS4_RCOM`, `CS3_ALERT`, `CS2_MISC`, `CS1_LOW` |
| 4 | Access Control → IPv4-Based ACE | Added 10 rules (all *Permit, protocol UDP, any address, destination port*): `CS7_NET`: 60056 and 514; `CS5_NAV`: 60002-60004; `CS4_RCOM`: 60006; `CS3_ALERT`: 60005, 60007, 60017-60020; `CS2_MISC`: 60001, 60057-60064; `CS1_LOW`: 60008-60016 |
| 5 | QoS Advanced Mode → Class Mapping | Created 6 class maps `C_CS7`, `C_CS5`, `C_CS4`, `C_CS3`, `C_CS2`, `C_CS1`, each matching its ACL |
| 6 | QoS Advanced Mode → Policy Table | Created policy **`PRIO450`** |
| 7 | QoS Advanced Mode → Policy Class Maps | In `PRIO450`, each class **sets DSCP**: C_CS7=56, C_CS5=40, C_CS4=32, C_CS3=24, C_CS2=16, C_CS1=8 |
| 8 | QoS Advanced Mode → Policy Binding | Bound `PRIO450` to **GE1, input**, default action **Permit Any** |

**Why "Permit Any"?** The default, "Deny Any", would have **dropped every packet that doesn't match a class**, including the ONF dump, ARP and IGMP. Permit Any lets unmatched traffic through untouched.

**Why match by port, not by address?** The ports map one-to-one to your groups (`600xx`), and ranges need far fewer rules than 30 separate addresses.

### The proof that it works

I sent all 30 groups **unmarked** (DSCP 0) from GE1 for 25 seconds:
- GE1 → GE9: 3,800 messages, **0 lost, 0 wrong DSCP on all 30 groups.** The switch gave every group its correct priority.
- GE9 → GE1: 3,800 "wrong DSCP", as expected, because there is **no policy on GE9's input**.

That test run is saved in `runs/_switch_validation/`.

---

## 9. Two things I did that you should know about

1. **An accidental click on Apply.** While trying to change the web page's *display mode* dropdown, my click landed on the **Apply** button of the "DSCP to Queue" page. The switch said "Success". I compared the table before and after: **every value was identical**, so it re-applied the same settings. It was not saved to the permanent configuration.
2. **Display mode changed to Advanced.** I set the web page's *Display Mode* (top right) from Basic to Advanced so the Advanced QoS menus show. It only changes how the page looks; set it back to Basic if you prefer.

---

## 10. Is it saved? How do I undo it?

**The switch changes are NOT saved to the startup configuration.** The red "save" icon at the top of the web page means "unsaved changes".

- **To keep them:** click the save icon (or *Administration → File Operations*, copy Running → Startup).
- **To discard them:** reboot the switch without saving. It loads the old configuration.
- **To undo by hand:** Policy Binding → clear GE1; then, if you want, set QoS Mode back to Basic. Note that switching modes can reset the QoS tables again.

Other things to clean up when you are done:
- Remove the passwordless-sudo rule: `sudo rm /etc/sudoers.d/net-test`
- Stop the monitoring stack: `scripts/stop_stack.sh`

---

## 11. How to run it yourself

```bash
cd /home/sav1023/Code/net/test

# one time per reboot: create the two network sandboxes
sudo scripts/netns_setup.sh
# one time ever (optional): no sudo password for the test
sudo scripts/enable_sudoless.sh

scripts/start_stack.sh                 # poller, exporter, Prometheus, Grafana, Loki
scripts/run_all.sh                     # all six marked profiles, about 70 minutes
PREFIX=pp_unmarked_ scripts/run_all.sh 100 150 200    # the unmarked versions
scripts/estop.sh                       # emergency stop
```

A quick self-test with no network and no sudo:
```bash
.venv/bin/python -m traffic.runner scenarios/pp_050mbps.json --local --duration 15
```
*(Do not do this while a real run is going: it briefly takes over the dashboard.)*

Open Grafana at `http://localhost:3000/d/pingpong-450`. Results are in `runs/`.

---

## 12. What is still to do

1. Run the **75, 100, 150 and 200 Mbit/s** marked profiles (the baseline).
2. For the comparison: run the **unmarked** versions with **no** switch policy, then with the **policy bound to GE1**. If the switch-classified results match the marked baseline, the switch-side fix works.
3. Decide whether to **save** the switch configuration.
4. Put the finished results into your **Zoho Sheet** as a new tab.
5. Consider a **gigabit NIC on GE9**: at 100 Mbit/s, the 100/150/200 tests mostly measure link saturation.

---

## 13. Known limits (so nothing surprises you)

- Live counters on the dashboard can differ for a moment; the final summary is exact.
- The ONF dump is sent as paced **UDP** (not TCP) so that loss can be measured; real ONF traffic is usually TCP.
- The live feed shows a sample, not every message.
- Red marks on SYSLOG lines in the feed are cosmetic (the text contains the word "error").
- I could not recalculate the Excel formulas here (no LibreOffice); Excel calculates Missing and Result when you open the file.
