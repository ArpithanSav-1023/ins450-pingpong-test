"""Excel results in the format of samples/mix.xlsx ("450 Test Sample Mix").

    .venv/bin/python tools/make_excel.py runs/<run>          # -> runs/<run>/results_<scenario>.xlsx
    .venv/bin/python tools/make_excel.py --all               # -> runs/all_profiles_summary.xlsx (one row per run and direction)
Sheets of the per-run workbook:
  "Groups and samples"   the template filled for GE1 -> GE9 (Received / Bad / Lost (n: gaps) / Wrong DSCP at GE9; Missing and Result by the template's own formulas)
  "Echo GE9 to GE1"      the same for the reply path (counted at GE1)
  "Loss summary"         message / packet / data loss, filler rate, switch port utilisation (from Prometheus) for both directions
Sent (expected) is the number the sender really sent (so a run stopped early is not reported as "missing"), Missing = sent - received.
"""
import glob
import json
import os
import sys
import urllib.parse
import urllib.request

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE = os.path.join(ROOT, "samples", "mix.xlsx")
SHEET = "Groups and samples"
PROM = "http://127.0.0.1:9090"
HEAD = PatternFill("solid", fgColor="DDEBF7")


def mb(n):
    """bytes -> megabytes (1 MB = 1,000,000 B)"""
    return round(n / 1e6, 3)


def port_stats(t0, t1, a="GE1", b="GE9"):
    """Average / max Mbit/s and utilisation % per switch port and direction over [t0, t1] from Prometheus (None if unreachable)."""
    out = {}
    gi = lambda n: "gi" + n[2:]          # "GE9" -> "gi9"
    for label, metric, port in [(f"{a} in ({a} sends)", "rx", gi(a)), (f"{a} out ({a} receives)", "tx", gi(a)),
                                (f"{b} in ({b} sends)", "rx", gi(b)), (f"{b} out ({b} receives)", "tx", gi(b))]:
        row = {}
        for key, q in [("mbps", f'cbs_port_{metric}_bps{{port="{port}"}}/1e6'), ("util", f'cbs_port_{metric}_util_percent{{port="{port}"}}')]:
            url = f"{PROM}/api/v1/query_range?" + urllib.parse.urlencode({"query": q, "start": t0, "end": t1, "step": 5})
            try:
                res = json.load(urllib.request.urlopen(url, timeout=5))["data"]["result"]
                v = [float(x[1]) for x in res[0]["values"]] if res else []
            except Exception:
                return None
            if v:
                v = v[1:-1] if len(v) > 4 else v      # drop the ramp-up / ramp-down samples
                row[key + "_avg"], row[key + "_max"] = sum(v) / len(v), max(v)
        out[label] = row
    return out


def fill_onf(ws, f_tx, f_rx):
    """ONF row (CCRS JSON, unicast 20100): the ONF dump that carries the bulk of the load."""
    r = next(r for r in range(13, 56) if ws.cell(r, 1).value == "CCRS JSON")
    ws.cell(r, 7).value, ws.cell(r, 8).value = "Yes", None
    ws.cell(r, 9).value = f_tx["tx_pkts"]
    ws.cell(r, 10).value = "ONF dump: chart data as paced UDP packets (1400 B payload), counted in packets"
    ws.cell(r, 12).value, ws.cell(r, 13).value, ws.cell(r, 14).value, ws.cell(r, 15).value = f_rx["rx_pkts"], 0, f_rx["rx_lost"], 0


def fill_sheet(ws, tx, rx, rate_hz, per_round_check=True):
    """tx: {group: {msgs, bytes}} of the sender; rx: {group: {msgs, lost, bad, wrong_dscp}} of the receiver."""
    rows = {ws.cell(r, 1).value: r for r in range(13, 56)}
    rows["Syslog"] = rows.get("Syslog")
    rounds = max([t["msgs"] // max(int(ws.cell(rows[g], 8).value or 1), 1) for g, t in tx.items() if g in rows] or [0])
    ws["B1"], ws["B2"] = rounds, rate_hz
    for g, r in rows.items():
        key = "SYSLOG" if g == "Syslog" else g
        if key in tx:
            ws.cell(r, 9).value = tx[key]["msgs"]                    # Samples in test = actually sent
            x = rx[key]
            ws.cell(r, 12).value, ws.cell(r, 13).value = x["msgs"], x["bad"]
            ws.cell(r, 14).value, ws.cell(r, 15).value = x["lost"], x["wrong_dscp"]
    ws["E1"], ws["E2"] = "pp.py (traffic/pp.py)", "pp.py (receives and counts in-band n: / block-id / PROCID counters)"
    ws["D3"] = "Filled automatically from the ping-pong run (stats_A.json / stats_B.json)"


def sheet_for(wb, title, template_ws):
    if title == SHEET:
        return template_ws
    ws = wb.copy_worksheet(template_ws)
    ws.title = title
    ws.freeze_panes = template_ws.freeze_panes
    for k, d in template_ws.column_dimensions.items():
        ws.column_dimensions[k].width = d.width
    for dv in template_ws.data_validations.dataValidation:
        ws.add_data_validation(dv)
    return ws


def summary_sheet(wb, o, ports, A):
    ws = wb.create_sheet("Loss summary")
    ws.append([f"Run {o['scenario']}: target {o['target_mbps']} Mbit/s ({o['target_mbps'] / 8:.3g} MB/s) per direction (mix + ONF dump, wire rate)"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(["message = one 450 sample; packet = any UDP datagram (mix + ONF dump); data = payload bytes. Loss comes from in-band counters; the totals use sender minus receiver."])
    ws.append([])
    hdr = ["Direction", "Messages sent", "Messages received", "Messages lost", "Message loss %", "Bad", "Wrong DSCP",
           "Packets sent", "Packets received", "Packets lost", "Packet loss %", "Data sent (B)", "Data received (B)", "Data lost (B)", "Data loss %",
           "ONF dump achieved (wire Mbit/s)", "Data sent (MB)", "Data received (MB)", "Data lost (MB)", "Mix payload sent (MB)", "ONF dump achieved (MB/s)"]
    ws.append(hdr)
    for c in ws[4]:
        c.font, c.fill = Font(bold=True), HEAD
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for d, v in o["directions"].items():
        m, p, b = v["messages"], v["packets"], v["data"]
        ws.append([d, m["sent"], m["recv"], m["lost"], round(m["loss_pct"], 4), m["bad"], m["wrong_dscp"], p["sent"], p["recv"], p["lost"], round(p["loss_pct"], 4),
                   b["sent_bytes"], b["recv_bytes"], b["lost_bytes"], round(b["loss_pct"], 4), v["filler"]["achieved_mbps"],
                   mb(b["sent_bytes"]), mb(b["recv_bytes"]), mb(b["lost_bytes"]), mb(m["sent_bytes"]), round(v["filler"]["achieved_mbps"] / 8, 3)])
    ws.append([])
    ws.append(["Switch port utilisation during the run (SNMP via Prometheus)"])
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    if ports:
        ws.append(["Port / direction", "Avg Mbit/s", "Max Mbit/s", "Avg util %", "Max util %", "Avg MB/s", "Max MB/s"])
        for c in ws[ws.max_row]:
            c.font, c.fill = Font(bold=True), HEAD
        for k, r in ports.items():
            ws.append([k] + [round(r.get(x, 0), 2) for x in ("mbps_avg", "mbps_max", "util_avg", "util_max")]
                       + [round(r.get("mbps_avg", 0) / 8, 3), round(r.get("mbps_max", 0) / 8, 3)])
    else:
        ws.append(["(Prometheus was not reachable, start the stack with scripts/start_stack.sh to include port utilisation)"])
    ws.column_dimensions["A"].width = 30
    for col in "BCDEFGHIJKLMNOPQRSTU":
        ws.column_dimensions[col].width = 16
    ws.row_dimensions[4].height = 45
    return ws


def build(run_dir):
    A = json.load(open(os.path.join(run_dir, "stats_A.json")))
    B = json.load(open(os.path.join(run_dir, "stats_B.json")))
    summ = json.load(open(os.path.join(run_dir, "summary.json")))
    hz = json.load(open(os.path.join(run_dir, "scenario.json"))).get("mix_hz", 10)
    scn = json.load(open(os.path.join(run_dir, "scenario.json")))
    pa, pb = scn.get("a_port", "GE1"), scn.get("b_port", "GE9")
    ports = port_stats(A["start"], A["start"] + (A.get("sent_secs") or A.get("duration_s") or 0) + 6, pa, pb)
    ports = ports or summ.get("ports")      # keep earlier port stats if Prometheus is down now
    summ["ports"] = ports
    json.dump(summ, open(os.path.join(run_dir, "summary.json"), "w"), indent=1)
    wb = openpyxl.load_workbook(TEMPLATE)
    ws1 = wb[SHEET]
    ws2 = sheet_for(wb, f"Echo {pb} to {pa}", ws1)
    fill_sheet(ws1, A["tx"], B["rx"], hz)
    fill_sheet(ws2, B["tx"], A["rx"], hz)
    fill_onf(ws1, A["filler"], B["filler"])
    fill_onf(ws2, B["filler"], A["filler"])
    ws1["A4"], ws2["A4"] = f"{pa} sends -> {pb} receives", f"{pb} echoes -> {pa} receives (reply path)"
    ws1["A10"], ws2["A10"] = "Direction (counts taken at the receiving port)", "Direction (counts taken at the receiving port)"
    summary_sheet(wb, summ, ports, A)
    wb.move_sheet("Dropdown options", offset=len(wb.sheetnames))   # keep the dropdown list sheet last
    out = os.path.join(run_dir, f"results_{summ['scenario']}.xlsx")
    wb.save(out)
    return out


def build_all(runs_dir):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "All profiles"
    hdr = ["Run", "Target Mbit/s (MB/s = Mbit/s / 8)", "Direction", "Msgs sent", "Msgs received", "Msgs lost", "Msg loss %", "Packets sent", "Packets received", "Packets lost", "Packet loss %",
           "Data lost (B)", "Data lost (MB)", "Data sent (MB)", "Data loss %", "ONF dump wire Mbit/s", "ONF dump MB/s", "Sender port in avg Mbit/s", "Sender port out avg Mbit/s", "Receiver port in avg Mbit/s", "Receiver port out avg Mbit/s",
           "Sender port in avg MB/s", "Receiver port out avg MB/s", "Receiver port out max util %"]
    ws.append(hdr)
    for c in ws[1]:
        c.font, c.fill = Font(bold=True), HEAD
        c.alignment = Alignment(wrap_text=True, vertical="top")
    for f in sorted(glob.glob(os.path.join(runs_dir, "*", "summary.json")), key=lambda p: json.load(open(p)).get("target_mbps", 0)):
        o = json.load(open(f))
        p = o.get("ports") or {}
        pv = list(p.values()) + [{}] * 4
        g = lambda i, x: round(pv[i].get(x, 0), 2)       # i: 0 sender in, 1 sender out, 2 receiver in, 3 receiver out
        for d, v in o["directions"].items():
            m, pk, b = v["messages"], v["packets"], v["data"]
            ws.append([os.path.basename(os.path.dirname(f)), o["target_mbps"], d, m["sent"], m["recv"], m["lost"], round(m["loss_pct"], 4), pk["sent"], pk["recv"], pk["lost"],
                       round(pk["loss_pct"], 4), b["lost_bytes"], mb(b["lost_bytes"]), mb(b["sent_bytes"]), round(b["loss_pct"], 4), v["filler"]["achieved_mbps"],
                       round(v["filler"]["achieved_mbps"] / 8, 3), g(0, "mbps_avg"), g(1, "mbps_avg"), g(2, "mbps_avg"),
                       g(3, "mbps_avg"), round(g(0, "mbps_avg") / 8, 3), round(g(3, "mbps_avg") / 8, 3),
                       g(3, "util_max")])
    ws.column_dimensions["A"].width = 34
    ws.row_dimensions[1].height = 45
    ws.freeze_panes = "A2"
    out = os.path.join(runs_dir, "all_profiles_summary.xlsx")
    wb.save(out)
    return out


if __name__ == "__main__":
    if sys.argv[1:] == ["--all"]:
        print(build_all(os.path.join(ROOT, "runs")))
    else:
        print(build(sys.argv[1]))
