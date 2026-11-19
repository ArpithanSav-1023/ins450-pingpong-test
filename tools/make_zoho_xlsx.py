"""Build the one-sheet results workbook (profile table + per-group loss table, with MB columns) that gets imported into Zoho Sheet.
Usage: .venv/bin/python tools/make_zoho_xlsx.py OUT.xlsx"""
import csv, glob, json, os, sys
import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEAD = PatternFill("solid", fgColor="D9E1F2")
mb = lambda n: round(n / 1e6, 3)

runs = []
for f in glob.glob(os.path.join(ROOT, "runs", "pp_0*", "summary.json")) + glob.glob(os.path.join(ROOT, "runs", "pp_1*", "summary.json")) + glob.glob(os.path.join(ROOT, "runs", "pp_2*", "summary.json")):
    o = json.load(open(f))
    runs.append((o["target_mbps"], os.path.dirname(f), o))
runs.sort(key=lambda r: r[0])

wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Ping-Pong Results"
ws.append(["IEC 61162-450 ping-pong test: results per profile (GE1 sends, GE9 echoes; 10 min each; marked traffic; GE9 link = 100 Mbps)"])
ws["A1"].font = Font(bold=True, size=13)
ws.append(["Mbps = Mbit per second, MB = megabyte (1 MB = 1,000,000 bytes, 1 MB/s = 8 Mbps), all per direction (mix + ONF dump, wire rate). Message = one 450 sample; packet = any UDP datagram; data = payload bytes."])
hdr = ["Profile (Mbps per direction)", "Messages sent", "Messages received", "Messages lost", "Message loss %", "Packets sent", "Packets received", "Packets lost", "Packet loss %",
       "Data loss %", "ONF dump wire Mbps", "GE1 in avg Mbps", "GE9 out avg Mbps", "GE9 out max util %",
       "Data sent (MB)", "Data received (MB)", "Data lost (MB)", "Mix payload sent (MB)", "ONF dump MB/s", "GE1 in avg MB/s", "GE9 out avg MB/s"]
ws.append(hdr)
for r, (t, d, o) in enumerate(runs):
    v = o["directions"]["GE1->GE9"]
    m, p, b = v["messages"], v["packets"], v["data"]
    pt = o.get("ports") or {}
    g = lambda k, x: (pt.get(k) or {}).get(x, 0)
    ws.append([t, m["sent"], m["recv"], m["lost"], round(m["loss_pct"], 3), p["sent"], p["recv"], p["lost"], round(p["loss_pct"], 3), round(b["loss_pct"], 3),
               v["filler"]["achieved_mbps"], round(g("GE1 in (GE1 sends)", "mbps_avg"), 1), round(g("GE9 out (GE9 receives)", "mbps_avg"), 1), round(g("GE9 out (GE9 receives)", "util_max"), 1),
               mb(b["sent_bytes"]), mb(b["recv_bytes"]), mb(b["lost_bytes"]), mb(m["sent_bytes"]), round(v["filler"]["achieved_mbps"] / 8, 3),
               round(g("GE1 in (GE1 sends)", "mbps_avg") / 8, 3), round(g("GE9 out (GE9 receives)", "mbps_avg") / 8, 3)])
for c in ws[3]:
    c.font, c.fill, c.alignment = Font(bold=True), HEAD, Alignment(wrap_text=True, vertical="top")
ws.row_dimensions[3].height = 45
ws.append([])
ws.append(["Message loss % per multicast group, GE1 to GE9"])
ws.cell(ws.max_row, 1).font = Font(bold=True, size=12)
ws.append(["Lowest-priority groups (CS1: PROP, USR1-8, same queue as the ONF dump) are the only ones that lose messages when the link saturates (100, 150, 200 Mbps)."])

# per-group: loss % per profile, messages sent, payload MB per run
rows = {}
order = []
for t, d, o in runs:
    for row in csv.DictReader(open(os.path.join(d, "summary_groups.csv"))):
        if row["direction"] != "GE1->GE9":
            continue
        k = row["group"]
        if k not in rows:
            rows[k] = {"sent": int(row["sent"]), "bytes": int(row["sent_bytes"]), "loss": {}}
            order.append(k)
        s, rc = int(row["sent"]), int(row["received"])
        rows[k]["loss"][t] = round(100 * (s - rc) / s, 1) if s else 0
sc = json.load(open(os.path.join(ROOT, "runs", os.path.basename(runs[0][1]), "scenario.json")))
info = {}
sys.path.insert(0, os.path.join(ROOT, "traffic"))
try:
    import sender450 as S
    for gname, rec in S.GROUPS.items() if isinstance(S.GROUPS, dict) else [(x[0], x) for x in S.GROUPS]:
        pass
except Exception:
    pass
prev = None
src = os.path.join(os.environ.get("PREV_XLSX", ""))
if src and os.path.exists(src):
    pw = openpyxl.load_workbook(src).active
    for r in pw.iter_rows(min_row=1, values_only=True):
        if r[0] and r[1] and str(r[1]).startswith("239."):
            info[r[0]] = (r[1], r[2])
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
