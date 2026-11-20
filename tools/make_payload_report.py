"""Excel report of payload test runs, one tab per scenario (for Zoho Sheet / Excel).

    .venv/bin/python tools/make_payload_report.py OUT.xlsx "Average case"=SENDER_RUN_DIR:RECEIVER_SUMMARY_DIR[:constant] [...]

Each tab has: the payload set-up, what was sent (messages/packets, data in MB and GB, rates in Mbit/s and MB/s), switch port utilisation
(Mbit/s, MB/s and % of the port speed, from Prometheus/SNMP on this PC), packet / message / data loss, a rate-over-time table (every 5 s over
the 10-minute test) with two line charts (rates, port utilisation %).
SENDER_RUN_DIR holds stats_A.json written by the sender; RECEIVER_SUMMARY_DIR holds summary.json and summary_groups.csv written by the receiver PC.
Add ":constant" for a run where every stream sent constantly at its maximum (no per-stream rate series exists for old runs, the maximums are used).
Rates: 8 Mbit/s = 1 MB/s. "Wire" rates include 46 bytes of Ethernet/IP/UDP headers per packet, "payload" rates do not.
"""
import csv
import json
import os
import sys
import urllib.parse
import urllib.request

import openpyxl
from openpyxl.chart import LineChart, Reference
from openpyxl.styles import Alignment, Font, PatternFill

PROM = "http://127.0.0.1:9090"
HEAD = PatternFill("solid", fgColor="DDEBF7")
SUB = PatternFill("solid", fgColor="F2F2F2")
GOOD = PatternFill("solid", fgColor="E2F0D9")
BAD = PatternFill("solid", fgColor="F8CBAD")
STREAMS = ["ONF_DB", "ECDIS", "ONF_OTHER"]
WIRE_PKT = 1446                 # wire bytes of one stream packet (1400 payload + 46)
RADAR_WIRE = 17624              # wire bytes of one 17160-byte radar datagram (12 IP fragments)
PORT_MBPS = {"gi1": 1000, "gi9": 1000}


def prom(expr, t0, t1, step=5):
    """[(t, value)] of one Prometheus range query (first series), [] if unavailable."""
    u = f"{PROM}/api/v1/query_range?" + urllib.parse.urlencode({"query": expr, "start": t0, "end": t1, "step": step})
    try:
        r = json.load(urllib.request.urlopen(u, timeout=15))["data"]["result"]
        return [(float(t), float(v)) for t, v in r[0]["values"]] if r else []
    except Exception:
        return []


def stat(vals):
    """(avg, max) of a list, ramp samples at both ends dropped."""
    v = vals[1:-1] if len(vals) > 4 else vals
    return (sum(v) / len(v), max(v)) if v else (0.0, 0.0)


def put(ws, r, c, v, bold=False, fill=None, fmt=None, wrap=False):
    cell = ws.cell(r, c, v)
    if bold:
        cell.font = Font(bold=True)
    if fill:
        cell.fill = fill
    if fmt:
        cell.number_format = fmt
    if wrap:
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    return cell


def header(ws, r, labels, c0=1):
    for i, h in enumerate(labels):
        put(ws, r, c0 + i, h, bold=True, fill=HEAD, wrap=True)


def tab(wb, name, sdir, rdir, constant):
    """Builds one scenario tab."""
    A = json.load(open(os.path.join(sdir, "stats_A.json")))
    S = json.load(open(os.path.join(rdir, "summary.json")))
    D = list(S["directions"].values())[0]
    groups = list(csv.DictReader(open(os.path.join(rdir, "summary_groups.csv"))))
    dur = A.get("sent_secs") or A.get("duration_s") or 600
    t0 = A["start"] + 8
    t1 = t0 + dur
    ws = wb.create_sheet(name)

    # ---------- series from Prometheus (every 5 s)
    q = {"in1": 'cbs_port_rx_bps{port="gi1"}/1e6', "util1": 'cbs_port_rx_util_percent{port="gi1"}',
         "out9": 'cbs_port_tx_bps{port="gi9"}/1e6', "util9": 'cbs_port_tx_util_percent{port="gi9"}',
         "sent": 'sum(rate(pp_side_msgs_sent{role="A",group!="RADAR"}[10s]))'}
    for s in STREAMS:
        q[s] = f'sum(rate(pp_side_stream_pkts_sent{{role="A",stream="{s}"}}[10s]))*{WIRE_PKT}*8/1e6'
    ser = {k: dict(prom(e, t0, t1)) for k, e in q.items()}
    ts = sorted(ser["in1"]) or sorted(ser["out9"])
    have_streams = all(ser[s] for s in STREAMS)
    mx = {s: next((x["mbps"] for x in A["streams"].values() if False), 0) for s in STREAMS}
    for s in STREAMS:
        mx[s] = A["streams"][s]["mbps"]

    # ---------- title
    put(ws, 1, 1, f"Payload test: {name}", bold=True).font = Font(bold=True, size=14)
    mode = ("every stream constantly AT its maximum rate" if constant else "rates vary between 25 % and 100 % of the maximum (average case); sentences 64 to 100 per second")
    put(ws, 2, 1, f"{dur:.0f} s, one way (sender to receiver, no echo). {mode}. 8 Mbit/s = 1 MB/s. Wire rate = payload + 46 bytes of headers per packet.")
    r = 4

    # ---------- 1. set-up
    put(ws, r, 1, "1. What the payload is (maximums per direction)", bold=True, fill=SUB); r += 1
    header(ws, r, ["Component", "Max Mbit/s (wire)", "Max MB/s", "Detail"]); r += 1
    setup = [("ONF database", mx["ONF_DB"], "unicast UDP :20100, 1400 B chart bytes, CS1"),
             ("ECDIS", mx["ECDIS"], "unicast UDP :20101, 1400 B chart bytes, CS1"),
             ("Other ONF", mx["ONF_OTHER"], "unicast UDP :20102, 1400 B chart bytes, CS1"),
             ("Radar (HALO20)", 0.65, "multicast 239.6.7.8:6678, 17160 B datagrams (12 IP fragments), CS2, about 4.6 datagrams/s"),
             ("450 sentences", 0.09, "30 multicast groups 239.192.0.x, 64 to 100 sentences/s, DSCP per group"),
             ("VDR", 0.0, "3 sentences per 5 s (group VDRD), fixed")]
    for n, m, dtl in setup:
        put(ws, r, 1, n); put(ws, r, 2, m, fmt="0.00"); put(ws, r, 3, f"=B{r}/8", fmt="0.000"); put(ws, r, 4, dtl); r += 1
    put(ws, r, 1, "Total maximum", bold=True); put(ws, r, 2, f"=SUM(B{r - 6}:B{r - 1})", bold=True, fmt="0.00"); put(ws, r, 3, f"=B{r}/8", bold=True, fmt="0.000")
    tot_row = r; r += 2

    # ---------- 2. what was sent
    put(ws, r, 1, "2. What was sent (counted by the sender)", bold=True, fill=SUB); r += 1
    header(ws, r, ["Component", "Packets / messages sent", "Data sent (payload MB)", "Data sent (payload GB)", "Average wire Mbit/s", "Average wire MB/s",
                   "Peak wire Mbit/s (5 s average)", "Peak wire MB/s"]); r += 1
    first = r
    peak = {}
    for s in STREAMS:
        if have_streams:
            vals = [ser[s].get(t, 0) for t in ts]
            peak[s] = stat(vals)[1]
        else:
            peak[s] = mx[s]
        st = A["streams"][s]
        put(ws, r, 1, {"ONF_DB": "ONF database", "ECDIS": "ECDIS", "ONF_OTHER": "Other ONF"}[s])
        put(ws, r, 2, st["tx_pkts"], fmt="#,##0"); put(ws, r, 3, round(st["tx_bytes"] / 1e6, 1), fmt="#,##0.0"); put(ws, r, 4, f"=C{r}/1000", fmt="0.00")
        put(ws, r, 5, round(st["tx_pkts"] * WIRE_PKT * 8 / 1e6 / dur, 2), fmt="0.00"); put(ws, r, 6, f"=E{r}/8", fmt="0.000")
        put(ws, r, 7, round(peak[s], 2), fmt="0.00"); put(ws, r, 8, f"=G{r}/8", fmt="0.000"); r += 1
    rad = A["tx"].get("RADAR", {"msgs": 0, "bytes": 0})
    put(ws, r, 1, "Radar"); put(ws, r, 2, rad["msgs"], fmt="#,##0"); put(ws, r, 3, round(rad["bytes"] / 1e6, 1), fmt="#,##0.0"); put(ws, r, 4, f"=C{r}/1000", fmt="0.00")
    put(ws, r, 5, round(rad["msgs"] * RADAR_WIRE * 8 / 1e6 / dur, 3), fmt="0.000"); put(ws, r, 6, f"=E{r}/8", fmt="0.0000"); put(ws, r, 7, 0.65, fmt="0.00"); put(ws, r, 8, f"=G{r}/8", fmt="0.0000"); r += 1
    sent_msgs = sum(v["msgs"] for k, v in A["tx"].items() if k != "RADAR")
    sent_bytes = sum(v["bytes"] for k, v in A["tx"].items() if k != "RADAR")
    put(ws, r, 1, "450 sentences (all groups, incl. VDR)"); put(ws, r, 2, sent_msgs, fmt="#,##0"); put(ws, r, 3, round(sent_bytes / 1e6, 3), fmt="#,##0.000"); put(ws, r, 4, f"=C{r}/1000", fmt="0.000")
    put(ws, r, 5, round((sent_bytes + 46 * sent_msgs) * 8 / 1e6 / dur, 3), fmt="0.000"); put(ws, r, 6, f"=E{r}/8", fmt="0.0000")
    sv = [ser["sent"].get(t, 0) for t in ts]
    put(ws, r, 7, "", fmt="0.00"); r += 1
    last = r - 1
    put(ws, r, 1, "TOTAL", bold=True)
    put(ws, r, 2, f"=SUM(B{first}:B{last})", bold=True, fmt="#,##0"); put(ws, r, 3, f"=SUM(C{first}:C{last})", bold=True, fmt="#,##0.0"); put(ws, r, 4, f"=C{r}/1000", bold=True, fmt="0.00")
    put(ws, r, 5, f"=SUM(E{first}:E{last})", bold=True, fmt="0.00"); put(ws, r, 6, f"=E{r}/8", bold=True, fmt="0.000")
    total_row = r; r += 1
    put(ws, r, 1, "Sentences per second (average / peak)"); put(ws, r, 2, round(sent_msgs / dur, 1), fmt="0.0")
    put(ws, r, 3, round(max(sv[1:-1]) if len(sv) > 4 else 0, 1), fmt="0.0"); r += 1
    put(ws, r, 1, "Percent of the maximum offered load"); put(ws, r, 5, f"=E{total_row}/B{tot_row}", fmt="0.0%"); r += 2

    # ---------- 3. port utilisation
    put(ws, r, 1, "3. Switch port utilisation (SNMP counters, 5 s samples; port speed 1000 Mbit/s)", bold=True, fill=SUB); r += 1
    header(ws, r, ["Port / direction", "Average Mbit/s", "Average MB/s", "Average utilisation %", "Peak Mbit/s", "Peak MB/s", "Peak utilisation %"]); r += 1
    for lbl, kb, ku in (("gi1 in (sender PC sends into the switch)", "in1", "util1"), ("gi9 out (switch sends to the receiver PC)", "out9", "util9")):
        vb = [ser[kb].get(t, 0) for t in ts]
        vu = [ser[ku].get(t, 0) for t in ts]
        ab, mb_ = stat(vb); au, mu = stat(vu)
        put(ws, r, 1, lbl); put(ws, r, 2, round(ab, 1), fmt="0.0"); put(ws, r, 3, f"=B{r}/8", fmt="0.00"); put(ws, r, 4, round(au, 1), fmt="0.0")
        put(ws, r, 5, round(mb_, 1), fmt="0.0"); put(ws, r, 6, f"=E{r}/8", fmt="0.00"); put(ws, r, 7, round(mu, 1), fmt="0.0"); r += 1
    put(ws, r, 1, "Switch counters include L2 frame bytes and anything else on the port; they are read every 5 s, so short peaks are smoothed.")
    ws.cell(r, 1).font = Font(italic=True); r += 2

    # ---------- 4. loss
    put(ws, r, 1, "4. Loss (sender count minus receiver count: exact)", bold=True, fill=SUB); r += 1
    header(ws, r, ["Item", "Sent", "Received", "Lost", "Loss %", "Note"]); r += 1
    rows = [("ONF database", D["streams"]["ONF_DB"]), ("ECDIS", D["streams"]["ECDIS"]), ("Other ONF", D["streams"]["ONF_OTHER"])]
    for n, x in rows:
        put(ws, r, 1, n); put(ws, r, 2, x["sent_pkts"], fmt="#,##0"); put(ws, r, 3, x["recv_pkts"], fmt="#,##0"); put(ws, r, 4, x["lost_pkts"], fmt="#,##0")
        put(ws, r, 5, f"=IF(B{r}=0,0,D{r}/B{r})", fmt="0.0000%"); put(ws, r, 6, "packets"); r += 1
    g = {x["group"]: x for x in groups}
    rd = g.get("RADAR")
    if rd:
        put(ws, r, 1, "Radar"); put(ws, r, 2, int(rd["sent"]), fmt="#,##0"); put(ws, r, 3, int(rd["received"]), fmt="#,##0"); put(ws, r, 4, int(rd["lost_exact"]), fmt="#,##0")
        put(ws, r, 5, f"=IF(B{r}=0,0,D{r}/B{r})", fmt="0.0000%"); put(ws, r, 6, "datagrams"); r += 1
    sen = [x for x in groups if x["group"] != "RADAR"]
    put(ws, r, 1, "450 sentences (30 groups)"); put(ws, r, 2, sum(int(x["sent"]) for x in sen), fmt="#,##0"); put(ws, r, 3, sum(int(x["received"]) for x in sen), fmt="#,##0")
    put(ws, r, 4, sum(int(x["lost_exact"]) for x in sen), fmt="#,##0"); put(ws, r, 5, f"=IF(B{r}=0,0,D{r}/B{r})", fmt="0.0000%")
    put(ws, r, 6, f"bad {sum(int(x['bad']) for x in sen)}, wrong DSCP {sum(int(x['wrong_dscp']) for x in sen)}"); r += 1
    m, p, b = D["messages"], D["packets"], D["data"]
    for n, sent, rec, lost, note in (("ALL messages (sentences + radar)", m["sent"], m["recv"], m["lost"], f"bad {m['bad']}, wrong DSCP {m['wrong_dscp']}"),
                                     ("ALL packets (messages + streams)", p["sent"], p["recv"], p["lost"], ""),
                                     ("ALL data (payload bytes)", b["sent_bytes"], b["recv_bytes"], b["lost_bytes"], "bytes")):
        put(ws, r, 1, n, bold=True); put(ws, r, 2, sent, fmt="#,##0"); put(ws, r, 3, rec, fmt="#,##0"); put(ws, r, 4, lost, fmt="#,##0")
        c = put(ws, r, 5, f"=IF(B{r}=0,0,D{r}/B{r})", fmt="0.0000%", bold=True); put(ws, r, 6, note)
        c.fill = GOOD if lost == 0 else BAD; r += 1
    r += 1

    # ---------- 5. time series + charts
    put(ws, r, 1, "5. Rates over the test (every 5 s)" + ("" if have_streams else "  (stream rates: constant at the maximum by design)"), bold=True, fill=SUB); r += 1
    hdr = ["Time (s)", "ONF database Mbit/s", "ECDIS Mbit/s", "Other ONF Mbit/s", "Streams total Mbit/s", "Port gi1 in Mbit/s", "Port gi9 out Mbit/s",
           "Time (s) ", "Port gi1 in util %", "Port gi9 out util %", "Sentences per s"]
    header(ws, r, hdr); hdr_row = r; r += 1
    base = ts[0] if ts else t0
    for t in ts:
        vals = [ser[s].get(t) if have_streams else mx[s] for s in ("ONF_DB", "ECDIS", "ONF_OTHER")]
        vals = [round(v, 2) if v is not None else None for v in vals]
        row = [round(t - base), *vals, round(sum(v or 0 for v in vals), 2), round(ser["in1"].get(t, 0), 1), round(ser["out9"].get(t, 0), 1),
               round(t - base), round(ser["util1"].get(t, 0), 2), round(ser["util9"].get(t, 0), 2), round(ser["sent"].get(t, 0), 1)]
        for i, v in enumerate(row):
            put(ws, r, 1 + i, v)
        r += 1
    n = len(ts)
    if n > 2:
        d0, d1 = hdr_row + 1, hdr_row + n
        cats = Reference(ws, min_col=1, min_row=d0, max_row=d1)
        c1 = LineChart(); c1.title = f"{name}: offered rates and port load (Mbit/s)"; c1.height, c1.width = 9, 22
        c1.y_axis.title = "Mbit/s (8 Mbit/s = 1 MB/s)"; c1.x_axis.title = "seconds into the test"
        for col in (2, 3, 4, 6, 7):
            c1.add_data(Reference(ws, min_col=col, min_row=hdr_row, max_row=d1), titles_from_data=True)
        c1.set_categories(cats)
        ws.add_chart(c1, f"L{hdr_row - 28 if hdr_row > 30 else 4}")
        c2 = LineChart(); c2.title = f"{name}: switch port utilisation (%)"; c2.height, c2.width = 9, 22
        c2.y_axis.title = "% of 1000 Mbit/s"; c2.x_axis.title = "seconds into the test"
        for col in (9, 10):
            c2.add_data(Reference(ws, min_col=col, min_row=hdr_row, max_row=d1), titles_from_data=True)
        c2.set_categories(Reference(ws, min_col=8, min_row=d0, max_row=d1))
        ws.add_chart(c2, f"L{(hdr_row - 28 if hdr_row > 30 else 4) + 19}")
    ws.column_dimensions["A"].width = 44
    for col in "BCDEFGHIJK":
        ws.column_dimensions[col].width = 18
    ws.freeze_panes = "A4"
    return ws


def main():
    out = sys.argv[1]
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for spec in sys.argv[2:]:
        name, rest = spec.split("=", 1)
        parts = rest.split(":")
        tab(wb, name, parts[0], parts[1], len(parts) > 2 and parts[2] == "constant")
    wb.save(out)
    print(out)


if __name__ == "__main__":
    main()
