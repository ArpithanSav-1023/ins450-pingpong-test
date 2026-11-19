"""Reads samples/mix.xlsx (450 Test Sample Mix) and writes scenarios/pp_<rate>mbps.json, one per test profile.

    .venv/bin/python tools/gen_scenarios.py
Groups = every row of the sheet where 'Sent by test sender' = Yes (multicast address, port, samples per round).
Profiles (target Mbit/s offered per direction, mix + ONF dump): 25 (under 50), 50, 75 (above 50), 100, 150, 200.
"""
import json
import os
import sys

import openpyxl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from traffic import sender450  # noqa: E402

PROFILES = [25, 50, 75, 100, 150, 200]
DURATION_S = 600
MAX_PER_SENSOR = 13   # a sensor (talker/SFI) may send at most 13 sentences per second


ws = openpyxl.load_workbook(os.path.join(ROOT, "samples", "mix.xlsx"), data_only=True)["Groups and samples"]
groups = []
for r in ws.iter_rows(values_only=True):
    if r[0] and isinstance(r[2], int) and r[6] == "Yes":
        name = "SYSLOG" if r[0] == "Syslog" else r[0]
        if name not in sender450.GROUPS:
            sys.exit(f"group {name} is in the sheet but not in traffic/sender450.py")
        assert sender450.GROUPS[name][0] == r[1] and sender450.GROUPS[name][1] == r[2], f"{name}: sheet and sender disagree"
        groups.append({"name": name, "addr": r[1], "port": r[2], "per_round": int(r[7]), "cos": sender450.GROUPS[name][2], "data_type": r[4], "header": r[5]})
# most sentences one sensor (SFI) sends per round -> rounds per second so that no sensor exceeds 13 sentences/s
from traffic import pp  # noqa: E402
_b, _st, _n = sender450.Builder(False), pp.Stamper(), {}
for g in groups:
    for p in sender450.payloads(g["name"], _b):
        m = pp.NM_TAG.match(_st.stamp(g["name"], p))
        if m:
            sfi = next(x for x in m.group(1).decode().split(",") if x.startswith("s:"))
            _n[sfi] = _n.get(sfi, 0) + 1
MIX_HZ = MAX_PER_SENSOR // max(_n.values())
print(f"busiest sensor: {max(_n, key=_n.get)[2:]} with {max(_n.values())} sentences/round -> mix_hz {MIX_HZ} ({max(_n.values()) * MIX_HZ}/s, limit {MAX_PER_SENSOR})")
os.makedirs(os.path.join(ROOT, "scenarios"), exist_ok=True)
for mbps in PROFILES:
    sc = {"name": f"pp_{mbps:03d}mbps", "duration_s": DURATION_S, "target_mbps": mbps, "mix_hz": MIX_HZ,
          "tx_ip": "192.168.50.1", "rx_ip": "192.168.50.2", "groups": groups,
          "_note": "A (GE1) sends the mix; B (GE9) echoes each datagram on the same group. ONF dump (chart data, unicast :20100) goes both ways. target_mbps = offered wire rate per direction."}
    json.dump(sc, open(os.path.join(ROOT, "scenarios", sc["name"] + ".json"), "w"), indent=1)
# same loads with every packet sent unmarked (DSCP 0): emulates a multiplexer that cannot set priority
for mbps in PROFILES:
    sc = json.load(open(os.path.join(ROOT, "scenarios", f"pp_{mbps:03d}mbps.json")))
    sc.update({"name": f"pp_unmarked_{mbps:03d}mbps", "marking": "none",
               "_note": "UNMARKED: the sender puts DSCP 0 on every packet (multiplexer that cannot mark). Compare with pp_<rate>mbps (marked) and with the same run after the switch classifies on ingress."})
    json.dump(sc, open(os.path.join(ROOT, "scenarios", sc["name"] + ".json"), "w"), indent=1)
print(f"{len(groups)} groups, {sum(g['per_round'] for g in groups)} messages/round, profiles {PROFILES} -> scenarios/")
