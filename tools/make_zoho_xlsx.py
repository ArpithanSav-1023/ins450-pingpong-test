"""Build the one-sheet results workbook (profile table + per-group loss table, with MB columns) that gets imported into Zoho Sheet.
Usage: .venv/bin/python tools/make_zoho_xlsx.py OUT.xlsx"""
import csv, glob, json, os, sys
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEAD = PatternFill("solid", fgColor="D9E1F2")
mb = lambda n: round(n / 1e6, 3)

runs = []
for f in glob.glob(os.path.join(os.environ.get("RUNS_DIR", os.path.join(ROOT, "runs")), "pp*", "summary.json")):
    o = json.load(open(f))
    runs.append((o["target_mbps"], os.path.dirname(f), o))
runs.sort(key=lambda r: r[0])

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Ping-Pong Results"
_d = next(iter(runs[0][2]["directions"])).split("->")
ws.append([f"IEC 61162-450 ping-pong test: results per profile ({_d[0]} sends from a separate PC, {_d[1]} echoes; 10 min each; marked traffic; sender link = 100 Mbps)"])
ws["A1"].font = Font(bold=True, size=13)
ws.append(["Mbps = Mbit per second, MB = megabyte (1 MB = 1,000,000 bytes, 1 MB/s = 8 Mbps), all per direction (mix + ONF dump, wire rate). Message = one 450 sample; packet = any UDP datagram; data = payload bytes."])
hdr = ["Profile (Mbps per direction)", "Messages sent", "Messages received", "Messages lost", "Message loss %", "Packets sent", "Packets received", "Packets lost", "Packet loss %",
       "Data loss %", "ONF dump wire Mbps", "Sender port in avg Mbps", "Receiver port out avg Mbps", "Receiver port out max util %",
       "Data sent (MB)", "Data received (MB)", "Data lost (MB)", "Mix payload sent (MB)", "ONF dump MB/s", "Sender port in avg MB/s", "Receiver port out avg MB/s"]
ws.append(hdr)
for r, (t, d, o) in enumerate(runs):
    dkey = next(iter(o["directions"]))
    v = o["directions"][dkey]
    m, p, b = v["messages"], v["packets"], v["data"]
    pt = o.get("ports") or {}
    pv = list(pt.values()) + [{}] * 4
    g = lambda i, x: pv[i].get(x, 0)
    ws.append([t, m["sent"], m["recv"], m["lost"], round(m["loss_pct"], 3), p["sent"], p["recv"], p["lost"], round(p["loss_pct"], 3), round(b["loss_pct"], 3),
               v["filler"]["achieved_mbps"], round(g(0, "mbps_avg"), 1), round(g(3, "mbps_avg"), 1), round(g(3, "util_max"), 1),
               mb(b["sent_bytes"]), mb(b["recv_bytes"]), mb(b["lost_bytes"]), mb(m["sent_bytes"]), round(v["filler"]["achieved_mbps"] / 8, 3),
               round(g(0, "mbps_avg") / 8, 3), round(g(3, "mbps_avg") / 8, 3)])
for c in ws[3]:
    c.font, c.fill, c.alignment = Font(bold=True), HEAD, Alignment(wrap_text=True, vertical="top")
ws.row_dimensions[3].height = 45
ws.append([])
ws.append([f"Message loss % per multicast group, {_d[0]} to {_d[1]}"])
ws.cell(ws.max_row, 1).font = Font(bold=True, size=12)
ws.append(["Lowest-priority groups (CS1: PROP, USR1-8, same queue as the ONF dump) are the only ones that lose messages when the link saturates (100, 150, 200 Mbps)."])

# per-group: loss % per profile, messages sent, payload MB per run
rows = {}
order = []
for t, d, o in runs:
    for row in csv.DictReader(open(os.path.join(d, "summary_groups.csv"))):
        if row["direction"] != next(iter(runs[0][2]["directions"])):
            continue
        k = row["group"]
        if k not in rows:
            rows[k] = {"sent": int(row["sent"]), "bytes": int(row["sent_bytes"]), "loss": {}}
            order.append(k)
        s, rc = int(row["sent"]), int(row["received"])
        rows[k]["loss"][t] = round(100 * (s - rc) / s, 1) if s else 0
sc0 = json.load(open(os.path.join(runs[0][1], "scenario.json")))
info = {g["name"]: (g["addr"], f"CS{g['cos']}") for g in sc0["groups"]}
ws.append(["Group", "Multicast address", "CoS (DSCP class)"] + [f"{t} Mbps lost %" for t, _, _ in runs] + ["Messages sent (per run)", "Payload sent (MB per run)"])
for c in ws[ws.max_row]:
    c.font, c.fill, c.alignment = Font(bold=True), HEAD, Alignment(wrap_text=True, vertical="top")
ws.row_dimensions[ws.max_row].height = 32
for k in order:
    a, cos = info.get(k, ("", ""))
    ws.append([k, a, cos] + [rows[k]["loss"].get(t, 0) for t, _, _ in runs] + [rows[k]["sent"], mb(rows[k]["bytes"])])
ws.column_dimensions["A"].width = 30
for col in "BCDEFGHIJKLMNOPQRSTU":
    ws.column_dimensions[col].width = 16
ws.freeze_panes = "B4"
wb.save(sys.argv[1])
print(sys.argv[1])
