"""Generates TWO separate Grafana dashboards from the per-side metrics (pp_side_*) of monitor/pp_exporter.py:
   pingpong450_sender.json   (role A: the separate sender PC, switch port GE9)   uid pingpong-450-sender
   pingpong450_receiver.json (role B: this PC, receiver + echo, switch port GE1) uid pingpong-450-receiver
Each shows only what that side knows itself (its own counters, its own port). Edit SENDER_PORT / RECEIVER_PORT if the cabling changes."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_dashboard as m
from make_dashboard import (BLUE, GREEN, GREY, ORANGE, RED, YELLOW, LOSS_T, UTIL_T, color_override, logs, pos, row, stat, table, target, text, timeseries,
                            util_gauge)

SENDER_PORT, RECEIVER_PORT = ("gi9", "GE9"), ("gi1", "GE1")


def build(role, title, uid, port, intro, labels):
    m.pid[0] = 0
    R = f'role="{role}"'
    P, PN = "$port", "${port}"       # a dashboard variable: pick the switch port this PC is on (either PC can be the sender or the receiver)
    default = port[0]
    sent, recv = labels["sent"], labels["recv"]
    panels = [
        text(intro, pos(0, 0, 13, 4)),
        stat("This side", f'pp_side_active{{{R}}}', pos(13, 0, 3, 4), mappings=[{"type": "value", "options": {"0": {"text": "idle", "color": GREY}, "1": {"text": "RUNNING", "color": GREEN}}}],
             thresholds=[(0, GREY), (1, GREEN)]),
        stat("Profile", f'pp_side_info{{{R}}}', pos(16, 0, 4, 4), text="name", legend="{{scenario}}", color="fixed"),
        stat("Sending for (s)", f'pp_side_sent_seconds{{{R}}}', pos(20, 0, 2, 4), color="fixed", decimals=0),
        stat("Switch polling", "cbs_poll_success", pos(22, 0, 2, 4), mappings=[{"type": "value", "options": {"0": {"text": "FAILING", "color": RED}, "1": {"text": "OK", "color": GREEN}}}],
             thresholds=[(0, RED), (1, GREEN)]),

        row(f"This side's switch port: {PN} (SNMP every 5 s)", 4),
        util_gauge(f"{PN} in (this side sends)", f'cbs_port_rx_util_percent{{port="{P}"}}', pos(0, 5, 4, 6), "Traffic from this side's host into the switch, % of the port speed."),
        util_gauge(f"{PN} out (this side receives)", f'cbs_port_tx_util_percent{{port="{P}"}}', pos(4, 5, 4, 6), "Traffic from the switch to this side's host."),
        timeseries(f"{PN} utilisation over time (%)", "Dashed: IEC 61162-460 80 % design limit and 90 % load alert.",
                   [target(f'cbs_port_rx_util_percent{{port="{P}"}}', "in", "A"), target(f'cbs_port_tx_util_percent{{port="{P}"}}', "out", "B")],
                   pos(8, 5, 16, 6), unit="percent", ymax=110, thresholds=[(80, YELLOW), (90, RED)],
                   overrides=[color_override("in", BLUE), color_override("out", ORANGE)]),
        timeseries(f"{PN} throughput: Mbit/s (MB/s in the legend)", "Wire rate seen by the switch on this side's port. 8 Mbit/s = 1 MB/s.",
                   [target(f'cbs_port_rx_bps{{port="{P}"}} / 1e6', "in (Mbit/s)", "A"), target(f'cbs_port_tx_bps{{port="{P}"}} / 1e6', "out (Mbit/s)", "B"),
                    target(f'cbs_port_rx_bps{{port="{P}"}} / 8e6', "in (MB/s)", "C"), target(f'cbs_port_tx_bps{{port="{P}"}} / 8e6', "out (MB/s)", "D")],
                   pos(0, 11, 24, 7), unit="Mbits", overrides=[color_override("in.*", BLUE), color_override("out.*", ORANGE)], mbs_legend=True),

        row("Message counters (this side only)", 11),
        stat(sent, f'sum(pp_side_messages_sent{{{R}}})', pos(0, 12, 6, 3), color="fixed", decimals=0),
        stat(recv, f'sum(pp_side_messages_received{{{R}}})', pos(6, 12, 6, 3), color="fixed", decimals=0),
        stat(labels["lost"], f'sum(pp_side_messages_lost{{{R}}})', pos(12, 12, 6, 3), thresholds=LOSS_T, decimals=0),
        stat("Bad / wrong DSCP", f'sum(pp_side_msgs_bad{{{R}}}) + sum(pp_side_msgs_wrong_dscp{{{R}}})', pos(18, 12, 6, 3), thresholds=LOSS_T, decimals=0),
        timeseries("Messages per second", "Mix messages this side sends and receives.",
                   [target(f'sum(clamp_min(deriv(pp_side_messages_sent{{{R}}}[10s]), 0))', sent, "A"), target(f'sum(clamp_min(deriv(pp_side_messages_received{{{R}}}[10s]), 0))', recv, "B")],
                   pos(0, 15, 12, 7), unit="short", overrides=[color_override(sent, BLUE), color_override(recv, ORANGE)]),
        timeseries("Packets per second (mix + ONF dump)", "All UDP datagrams this side sends and receives.",
                   [target(f'clamp_min(deriv(pp_side_packets_sent{{{R}}}[10s]), 0)', sent, "A"), target(f'clamp_min(deriv(pp_side_packets_received{{{R}}}[10s]), 0)', recv, "B")],
                   pos(12, 15, 12, 7), unit="pps", overrides=[color_override(sent, BLUE), color_override(recv, ORANGE)]),

        row("Loss as seen by this side", 22),
        stat("Packets missing (mix + ONF)", f'sum(pp_side_packets_lost{{{R}}})', pos(0, 23, 6, 3), thresholds=LOSS_T, decimals=0),
        stat("ONF dump packets missing", f'sum(pp_side_filler_pkts_lost{{{R}}})', pos(6, 23, 6, 3), thresholds=LOSS_T, decimals=0),
        stat("ONF packets received", f'sum(pp_side_filler_pkts_received{{{R}}})', pos(12, 23, 6, 3), color="fixed", decimals=0),
        stat("ONF packets sent", f'sum(pp_side_filler_pkts_sent{{{R}}})', pos(18, 23, 6, 3), color="fixed", decimals=0),
        timeseries("Missing messages per group", "Sequence gaps this side saw, per multicast group.", [target(f'pp_side_msgs_lost{{{R}}}', "{{group}}")], pos(0, 26, 12, 7), unit="short"),
        timeseries("Missing packets over time", "Mix + ONF dump packets this side saw missing.", [target(f'sum(pp_side_packets_lost{{{R}}})', "packets"), target(f'sum(pp_side_messages_lost{{{R}}})', "messages", "B")],
                   pos(12, 26, 12, 7), unit="short"),

        row("Per multicast group (this side)", 33),
        table("Per group", pos(0, 34, 24, 12),
              [("A", f'sum by(group) (pp_side_msgs_sent{{{R}}})'), ("B", f'sum by(group) (pp_side_msgs_received{{{R}}})'), ("C", f'sum by(group) (pp_side_msgs_lost{{{R}}})')]),

        row("Live message feed (this side; sampled about once per group per 2 s, losses always shown)", 46),
        logs(f"{labels['feed']}", labels["feed_desc"], f'{{job="pingpong",role="{role}",kind=~"TX|RX"}}', pos(0, 47, 24, 11)),
        logs("Lost / bad / wrong-DSCP messages (this side, every event)", "Empty means nothing was lost.", f'{{job="pingpong",role="{role}",kind=~"LOSS|BAD|DSCP"}}', pos(0, 58, 24, 5)),
    ]
    for p_ in panels:                                     # the throughput panel got its own full-width row at y=11: push everything below it down
        if p_["gridPos"]["y"] >= 11 and "throughput" not in p_.get("title", ""):
            p_["gridPos"]["y"] += 7
    # streams of the payload profile (ECDIS, ONF database, other ONF): unicast UDP, counted in packets
    yb = max(p_["gridPos"]["y"] + p_["gridPos"]["h"] for p_ in panels)
    sent_m = f'sum by(stream) (clamp_min(deriv(pp_side_stream_pkts_sent{{{R}}}[10s]), 0)) * 1446 * 8 / 1e6'
    recv_m = f'sum by(stream) (clamp_min(deriv(pp_side_stream_pkts_received{{{R}}}[10s]), 0)) * 1446 * 8 / 1e6'
    tbl = table("Per stream (this side)", pos(0, yb + 1, 10, 8),
                [("A", f'sum by(stream) (pp_side_stream_pkts_sent{{{R}}})'), ("B", f'sum by(stream) (pp_side_stream_pkts_received{{{R}}})'), ("C", f'sum by(stream) (pp_side_stream_pkts_lost{{{R}}})')])
    tbl["transformations"][1]["options"]["indexByName"] = {"stream": 0}
    tbl["options"]["sortBy"] = [{"displayName": "stream", "desc": False}]
    panels += [row("Payload streams: ECDIS / ONF database / other ONF (packets; MB/s in brackets)", yb), tbl,
               timeseries("Stream throughput: Mbit/s sent and received by this side", "Wire rate per stream, from packet counters (1446 B on the wire per packet). 8 Mbit/s = 1 MB/s.",
                          [target(sent_m, "{{stream}} sent", "A"), target(recv_m, "{{stream}} received", "B")], pos(10, yb + 1, 14, 8), unit="Mbits")]
    if role == "A":       # "Echo packet loss": everything the receiver sent back (echo of the mix, radar, ONF dump) that never arrived here
        y0 = next(p_["gridPos"]["y"] for p_ in panels if p_.get("title", "").startswith("Per multicast group"))
        for p_ in panels:
            if p_["gridPos"]["y"] >= y0:
                p_["gridPos"]["y"] += 11
        lost = f'sum(pp_side_packets_lost{{{R}}})'
        got = f'sum(pp_side_packets_received{{{R}}})'
        panels += [
            row("Echo packet loss (packets the receiver sent back that never reached this PC)", y0),
            stat("Echo packets lost (mix + radar + ONF)", lost, pos(0, y0 + 1, 6, 3), thresholds=LOSS_T, decimals=0),
            stat("Echo packet loss %", f'100 * {lost} / clamp_min({lost} + {got}, 1)', pos(6, y0 + 1, 6, 3), unit="percent", thresholds=[(0, GREEN), (0.0001, RED)], decimals=4),
            stat("Radar echoes lost", f'sum(pp_side_msgs_lost{{{R},group="RADAR"}}) or vector(0)', pos(12, y0 + 1, 6, 3), thresholds=LOSS_T, decimals=0),
            stat("Mix echoes not back (sent - echoed)", f'sum(pp_side_messages_sent{{{R}}}) - sum(pp_side_messages_received{{{R}}})', pos(18, y0 + 1, 6, 3), thresholds=LOSS_T, decimals=0),
            timeseries("Echo packet loss % over time", "Share of the echo packets that went missing, per 30 s window.",
                       [target(f'100 * clamp_min(increase(pp_side_packets_lost{{{R}}}[30s]), 0) / clamp_min(increase(pp_side_packets_lost{{{R}}}[30s]) + increase(pp_side_packets_received{{{R}}}[30s]), 1)', "echo packet loss %")],
                       pos(0, y0 + 4, 12, 7), unit="percent", thresholds=[(0.0001, RED)]),
            timeseries("Echo packets lost per second", "Missing echo packets per second: all, ONF dump only, and mix messages (radar included).",
                       [target(f'clamp_min(deriv(pp_side_packets_lost{{{R}}}[10s]), 0)', "all echo packets", "A"), target(f'clamp_min(deriv(pp_side_filler_pkts_lost{{{R}}}[10s]), 0)', "ONF dump", "B"),
                        target(f'sum(clamp_min(deriv(pp_side_msgs_lost{{{R},group="RADAR"}}[10s]), 0))', "radar", "C"), target(f'sum(clamp_min(deriv(pp_side_messages_lost{{{R}}}[10s]), 0))', "mix + radar messages", "D")],
                       pos(12, y0 + 4, 12, 7), unit="pps"),
        ]
    if role == "A":       # selection list (tools/selector.py on :9104): which sentences / groups / radar the sender sends; changes apply to a running test
        yb = max(p_["gridPos"]["y"] + p_["gridPos"]["h"] for p_ in panels)      # at the bottom of the dashboard
        panels.append(row("Choose what the sender sends (tick = sent, applies within about a second)", yb))
        panels.append({"id": m.nxt(), "type": "text", "title": "Sender selection", "gridPos": pos(0, yb + 1, 24, 16),
                       "options": {"mode": "html", "content": '<iframe src="http://localhost:9104/" style="width:100%;height:100%;border:0"></iframe>'}})
    return {"uid": uid, "title": title, "tags": ["450", "cbs350", role == "A" and "sender" or "receiver"], "schemaVersion": 39, "version": 1,
            "editable": True, "refresh": "5s", "time": {"from": "now-15m", "to": "now"}, "timezone": "browser", "graphTooltip": 1,
            "annotations": {"list": [{"datasource": m.DS, "enable": True, "name": "This side running", "iconColor": "rgba(130,130,130,0.35)", "expr": f'pp_side_active{{{R}}} == 1',
                                      "step": "5s", "titleFormat": "Running", "useValueForTime": False}]},
            "panels": panels,
            "templating": {"list": [{"name": "port", "label": "This PC's switch port (auto)", "type": "query", "datasource": m.DS, "query": {"query": "label_values(pp_this_pc_port, port)", "refId": "p"},
                                     "refresh": 2, "multi": False, "includeAll": False, "hide": 0, "sort": 0,
                                     "current": {"text": default, "value": default}, "options": []}]}}


SENDER = build("A", "450 Ping-Pong: SENDER", "pingpong-450-sender", SENDER_PORT,
               "## SENDER side  \nThis is the **PC that runs `scripts/sender.sh`** (role A). Its switch port is detected automatically (shown at the top: gi9 = port GE9, gi1 = port GE1). It sends the 450 sample mix on every multicast group plus the ONF dump, and counts what **comes back** (the echo).  \n"
               "Everything here is taken from the sender's **own** counters and its **own** switch port. *Missing* = messages that came back with a gap in the sequence numbers.",
               {"sent": "Sent", "recv": "Echoes received", "lost": "Echoes missing", "feed": "Sender: sent (TX) and echo received (RX)",
                "feed_desc": "TX = this side sends the mix, RX = the echo coming back. Newest first."})
RECEIVER = build("B", "450 Ping-Pong: RECEIVER + ECHO", "pingpong-450-receiver", RECEIVER_PORT,
                 "## RECEIVER side  \nThis is the **PC that runs `scripts/receiver.sh`** (role B). Its switch port is detected automatically (shown at the top: gi9 = port GE9, gi1 = port GE1). It **receives** the sender's mix and ONF dump, counts loss from the sequence numbers, and **echoes** every datagram back on the same group.  \n"
                 "Everything here is taken from the receiver's **own** counters and its **own** switch port. *Missing* = messages that arrived with a gap in the sequence numbers.",
                 {"sent": "Echoed (sent back)", "recv": "Received", "lost": "Missing on arrival", "feed": "Receiver: received (RX) and echoed (TX)",
                  "feed_desc": "RX = mix arriving from the sender, TX = echo sent back. Newest first."})

for d, f in ((SENDER, "pingpong450_sender.json"), (RECEIVER, "pingpong450_receiver.json")):
    out = os.path.join(m.ROOT, "config/grafana/dashboards", f)
    json.dump(d, open(out, "w"), indent=1)
    print(out, len(d["panels"]), "panels")
