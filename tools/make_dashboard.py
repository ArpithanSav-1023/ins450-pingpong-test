"""Generates the Grafana dashboard JSON (config/grafana/dashboards/pingpong450.json). Re-run after editing."""
import json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DS = {"type": "prometheus", "uid": "prom"}
BLUE, ORANGE, AQUA, GREY, GREEN, RED, YELLOW = "#3987e5", "#d95926", "#199e70", "#8e949e", "#37b24d", "#e03131", "#f2a900"
pid = [0]


def nxt():
    pid[0] += 1
    return pid[0]


def color_override(regex, color, dash=None):
    props = [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]
    if dash:
        props.append({"id": "custom.lineStyle", "value": {"fill": "dash", "dash": dash}})
    return {"matcher": {"id": "byRegexp", "options": regex}, "properties": props}


def target(expr, legend="", ref="A", instant=False):
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}
    if instant:
        t.update({"instant": True, "range": False})
    return t


def pos(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def timeseries(title, desc, targets, grid, unit="short", overrides=(), ymax=None, thresholds=None, ymin=0, mbs_legend=False):
    d = {"color": {"mode": "palette-classic"}, "unit": unit, "min": ymin,
         "custom": {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never", "spanNulls": False, "thresholdsStyle": {"mode": "line" if thresholds else "off"}}}
    if ymax:
        d["max"] = ymax
    if thresholds:
        d["thresholds"] = {"mode": "absolute", "steps": [{"color": "transparent", "value": None}] + [{"color": c, "value": v} for v, c in thresholds]}
    ov = list(overrides)
    legend = {"displayMode": "list", "placement": "bottom", "showLegend": True}
    if mbs_legend:   # series named "... (MB/s)" are not drawn (same curve as Mbit/s, just /8) but show up in the legend table and the tooltip
        ov.append({"matcher": {"id": "byRegexp", "options": ".*MB/s.*"},
                   "properties": [{"id": "custom.hideFrom", "value": {"viz": True, "legend": False, "tooltip": False}}, {"id": "unit", "value": "suffix: MB/s"}, {"id": "decimals", "value": 2}]})
        ov.append({"matcher": {"id": "byRegexp", "options": ".*Mbit/s.*"}, "properties": [{"id": "decimals", "value": 1}]})
        legend = {"displayMode": "table", "placement": "right", "showLegend": True, "calcs": ["lastNotNull", "max"], "width": 330}
    return {"id": nxt(), "type": "timeseries", "title": title, "description": desc, "datasource": DS, "gridPos": grid, "targets": targets,
            "fieldConfig": {"defaults": d, "overrides": ov},
            "options": {"legend": legend, "tooltip": {"mode": "multi", "sort": "desc"}}}


def stat(title, expr, grid, unit="none", thresholds=None, mappings=None, text="auto", legend="", color="thresholds", instant=True, decimals=None):
    d = {"unit": unit, "color": {"mode": color}, "mappings": mappings or []}
    if thresholds:
        d["thresholds"] = {"mode": "absolute", "steps": [{"color": thresholds[0][1], "value": None}] + [{"color": c, "value": v} for v, c in thresholds[1:]]}
    else:
        d["thresholds"] = {"mode": "absolute", "steps": [{"color": BLUE, "value": None}]}
    if decimals is not None:
        d["decimals"] = decimals
    return {"id": nxt(), "type": "stat", "title": title, "datasource": DS, "gridPos": grid, "targets": [target(expr, legend, instant=instant)],
            "fieldConfig": {"defaults": d, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": "background", "graphMode": "none", "textMode": text, "justifyMode": "center"}}


def gauge(title, expr, grid):
    return {"id": nxt(), "type": "gauge", "title": title, "datasource": DS, "gridPos": grid, "targets": [target(expr, instant=True)],
            "fieldConfig": {"defaults": {"unit": "percent", "min": 0, "max": 100, "color": {"mode": "thresholds"},
                                         "thresholds": {"mode": "absolute", "steps": [{"color": GREEN, "value": None}, {"color": YELLOW, "value": 60}, {"color": RED, "value": 90}]}}, "overrides": []},
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "showThresholdMarkers": True}}


def bars(title, desc, targets, grid, unit="percent", maxv=None, overrides=()):
    d = {"unit": unit, "min": 0, "color": {"mode": "fixed", "fixedColor": BLUE},
         "thresholds": {"mode": "absolute", "steps": [{"color": BLUE, "value": None}]}}
    if maxv:
        d["max"] = maxv
    return {"id": nxt(), "type": "bargauge", "title": title, "description": desc, "datasource": DS, "gridPos": grid, "targets": targets,
            "fieldConfig": {"defaults": d, "overrides": list(overrides)},
            "options": {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True, "valueMode": "color",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}


def text(content, grid):
    return {"id": nxt(), "type": "text", "title": "", "gridPos": grid, "options": {"mode": "markdown", "content": content}}


# twin colouring: unmarked (_be) = orange, background cameras = grey, marked = blue shades
UNM = [color_override(".*_be.*", ORANGE), color_override("nav_gnss$", BLUE), color_override("nav_heading$", "#74c0fc"), color_override("cam_af31", "#339af0"),
       color_override("cam_bg.*", GREY), color_override("sat_telemetry", GREEN)]
UTIL_T = [(0, GREEN), (80, YELLOW), (90, RED)]   # IEC 61162-460: 80 % design limit (5.4.2), 90 % load alert (8.2.2)


def row(title, y):
    return {"id": nxt(), "type": "row", "title": title, "collapsed": False, "gridPos": pos(0, y, 24, 1), "panels": []}


def util_gauge(title, expr, grid, desc):
    g = gauge(title, expr, grid)
    g["description"] = desc
    g["fieldConfig"]["defaults"]["thresholds"]["steps"] = [{"color": c, "value": (None if v == 0 else v)} for v, c in UTIL_T]
    g["fieldConfig"]["defaults"]["decimals"] = 0
    return g


def port_bars(title, desc, expr, grid):
    b = bars(title, desc, [target(expr, "{{port}}", instant=True)], grid, maxv=100)
    b["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
    b["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": [{"color": c, "value": (None if v == 0 else v)} for v, c in UTIL_T]}
    b["fieldConfig"]["defaults"]["decimals"] = 0
    b["options"]["displayMode"] = "lcd"
    return b



D1, D2 = "ge1_to_ge9", "ge9_to_ge1"
DIRC = [color_override(".*GE1.?.?GE9.*|.*ge1_to_ge9.*", BLUE), color_override(".*GE9.?.?GE1.*|.*ge9_to_ge1.*", ORANGE)]
PORTC = [color_override("GE1 in.*", BLUE), color_override("GE1 out.*", "#74c0fc"), color_override("GE9 in.*", ORANGE), color_override("GE9 out.*", "#ffa94d")]
LOSS_T = [(0, GREEN), (0.0001, YELLOW), (1, RED)]


def sumd(metric, d):
    return f'sum(pp_{metric}{{direction="{d}"}})'


def table(title, grid, exprs):
    return {"id": nxt(), "type": "table", "title": title, "datasource": DS, "gridPos": grid,
            "targets": [{"datasource": DS, "expr": e, "format": "table", "instant": True, "range": False, "refId": r} for r, e in exprs],
            "transformations": [{"id": "merge"}, {"id": "organize", "options": {
                "excludeByName": {"Time": True}, "indexByName": {"group": 0}, "renameByName": {"Value #A": "sent", "Value #B": "received", "Value #C": "lost"}}}],
            "fieldConfig": {"defaults": {"custom": {"align": "right"}}, "overrides": []},
            "options": {"showHeader": True, "sortBy": [{"displayName": "group", "desc": False}]}}


def target_stat(grid):
    st = stat("Target per direction", "pp_target_mbps", grid, unit="none", color="fixed", legend="Mbit/s")
    st["targets"].append(target("pp_target_mbps / 8", "MB/s", "B", instant=True))
    st["options"]["textMode"] = "value_and_name"
    st["fieldConfig"]["defaults"]["decimals"] = 2
    return st


panels = [
    text("## IEC 61162-450 ping-pong test  \n"
         "**GE1 sends** the 450 sample mix on every multicast group, **GE9 receives and echoes** each datagram back on the same group, GE1 receives the reply, and this repeats for the whole run. "
         "The **ONF dump** (chart data, unicast port 20100) goes both ways and carries the bulk of the load, because one sensor may only send 13 sentences/s.  \n"
         "**message** = one 450 sample (sentence / binary block / syslog line) &nbsp; **packet** = any UDP datagram (mix + ONF dump) &nbsp; **data** = payload bytes. "
         "Loss comes from in-band sequence numbers. Port utilisation comes from the switch (SNMP, 5 s).", pos(0, 0, 13, 4)),
    stat("Test", "pp_run_active", pos(13, 0, 3, 4), mappings=[{"type": "value", "options": {"0": {"text": "idle", "color": GREY}, "1": {"text": "RUNNING", "color": GREEN}}}],
         thresholds=[(0, GREY), (1, GREEN)]),
    stat("Profile", "pp_run_info", pos(16, 0, 4, 4), text="name", legend="{{scenario}}", color="fixed"),
    target_stat(pos(20, 0, 2, 4)),
    stat("Switch polling", "cbs_poll_success", pos(22, 0, 2, 4), mappings=[{"type": "value", "options": {"0": {"text": "FAILING", "color": RED}, "1": {"text": "OK", "color": GREEN}}}],
         thresholds=[(0, RED), (1, GREEN)]),

    row("Port utilisation (switch, SNMP every 5 s)", 4),
    util_gauge("GE1 in (GE1 sends)", 'cbs_port_rx_util_percent{port="gi1"}', pos(0, 5, 4, 6), "Traffic from the GE1 host into the switch, % of the port speed."),
    util_gauge("GE1 out (GE1 receives)", 'cbs_port_tx_util_percent{port="gi1"}', pos(4, 5, 4, 6), "Traffic from the switch to the GE1 host (GE9's echo and filler)."),
    util_gauge("GE9 in (GE9 sends)", 'cbs_port_rx_util_percent{port="gi9"}', pos(8, 5, 4, 6), "Traffic from the GE9 host into the switch."),
    util_gauge("GE9 out (GE9 receives)", 'cbs_port_tx_util_percent{port="gi9"}', pos(12, 5, 4, 6), "Traffic from the switch to the GE9 host. If GE9 negotiated 100 Mbit/s this saturates at 100 % first."),
    timeseries("Utilisation over time (%), IEC 61162-460 limits", "Dashed: 80 % design limit and 90 % 'Network load' alert.",
               [target('cbs_port_rx_util_percent{port="gi1"}', "GE1 in", "A"), target('cbs_port_tx_util_percent{port="gi1"}', "GE1 out", "B"),
                target('cbs_port_rx_util_percent{port="gi9"}', "GE9 in", "C"), target('cbs_port_tx_util_percent{port="gi9"}', "GE9 out", "D")],
               pos(16, 5, 8, 6), unit="percent", ymax=110, thresholds=[(80, YELLOW), (90, RED)], overrides=PORTC),
    timeseries("Port throughput: Mbit/s (MB/s in the legend and tooltip)", "Wire rate seen by the switch, per port and direction. Compare with the target. 8 Mbit/s = 1 MB/s.",
               [target('cbs_port_rx_bps{port="gi1"} / 1e6', "GE1 in (Mbit/s)", "A"), target('cbs_port_tx_bps{port="gi1"} / 1e6', "GE1 out (Mbit/s)", "B"),
                target('cbs_port_rx_bps{port="gi9"} / 1e6', "GE9 in (Mbit/s)", "C"), target('cbs_port_tx_bps{port="gi9"} / 1e6', "GE9 out (Mbit/s)", "D"),
                target('cbs_port_rx_bps{port="gi1"} / 8e6', "GE1 in (MB/s)", "E"), target('cbs_port_tx_bps{port="gi1"} / 8e6', "GE1 out (MB/s)", "F"),
                target('cbs_port_rx_bps{port="gi9"} / 8e6', "GE9 in (MB/s)", "G"), target('cbs_port_tx_bps{port="gi9"} / 8e6', "GE9 out (MB/s)", "H")],
               pos(0, 11, 24, 7), unit="Mbits", overrides=PORTC, mbs_legend=True),

    row("Message counters (sender and receiver, each side)", 18),
    stat("GE1 sent", sumd("messages_sent", D1), pos(0, 19, 6, 3), color="fixed", decimals=0),
    stat("GE9 received", sumd("messages_received", D1), pos(6, 19, 6, 3), color="fixed", decimals=0),
    stat("GE9 sent (echo)", sumd("messages_sent", D2), pos(12, 19, 6, 3), color="fixed", decimals=0),
    stat("GE1 received (reply)", sumd("messages_received", D2), pos(18, 19, 6, 3), color="fixed", decimals=0),
    timeseries("Messages per second", "Sent by one side vs received by the other.",
               [target(f'sum(clamp_min(deriv(pp_messages_sent{{direction="{D1}"}}[10s]), 0))', "GE1 sent", "A"), target(f'sum(clamp_min(deriv(pp_messages_received{{direction="{D1}"}}[10s]), 0))', "GE9 received", "B"),
                target(f'sum(clamp_min(deriv(pp_messages_sent{{direction="{D2}"}}[10s]), 0))', "GE9 sent (echo)", "C"), target(f'sum(clamp_min(deriv(pp_messages_received{{direction="{D2}"}}[10s]), 0))', "GE1 received", "D")],
               pos(0, 22, 12, 7), unit="short",
               overrides=[color_override("GE1 sent", BLUE), color_override("GE9 received", "#74c0fc"), color_override("GE9 sent.*", ORANGE), color_override("GE1 received", "#ffa94d")]),
    timeseries("Packets per second (mix + ONF dump)", "All UDP datagrams: the 450 mix plus the ONF dump.",
               [target(f'clamp_min(deriv(pp_packets_sent{{direction="{D1}"}}[10s]), 0)', "GE1 sent", "A"), target(f'clamp_min(deriv(pp_packets_received{{direction="{D1}"}}[10s]), 0)', "GE9 received", "B"),
                target(f'clamp_min(deriv(pp_packets_sent{{direction="{D2}"}}[10s]), 0)', "GE9 sent", "C"), target(f'clamp_min(deriv(pp_packets_received{{direction="{D2}"}}[10s]), 0)', "GE1 received", "D")],
               pos(12, 22, 12, 7), unit="pps",
               overrides=[color_override("GE1 sent", BLUE), color_override("GE9 received", "#74c0fc"), color_override("GE9 sent", ORANGE), color_override("GE1 received", "#ffa94d")]),

    row("Loss", 29),
    stat("Messages lost", "pp_messages_lost", pos(0, 30, 6, 3), legend="{{direction}}", thresholds=LOSS_T, decimals=0),
    stat("Packets lost", "pp_packets_lost", pos(6, 30, 6, 3), legend="{{direction}}", thresholds=LOSS_T, decimals=0),
    stat("Data lost (bytes)", "pp_data_bytes_lost", pos(12, 30, 6, 3), legend="{{direction}}", unit="bytes", thresholds=LOSS_T, decimals=0),
    stat("Wrong DSCP / bad messages", "sum by(direction) (pp_msgs_wrong_dscp) + sum by(direction) (pp_msgs_bad)", pos(18, 30, 6, 3), legend="{{direction}}", thresholds=LOSS_T, decimals=0),
    timeseries("Message loss (%)", "450 samples lost, from sequence-number gaps.", [target("pp_message_loss_percent", "{{direction}}")], pos(0, 33, 8, 7), unit="percent", overrides=DIRC),
    timeseries("Packet loss (%)", "All UDP datagrams (mix + ONF dump).", [target("pp_packet_loss_percent", "{{direction}}")], pos(8, 33, 8, 7), unit="percent", overrides=DIRC),
    timeseries("Data loss (%)", "Payload bytes lost / payload bytes sent.", [target("pp_data_loss_percent", "{{direction}}")], pos(16, 33, 8, 7), unit="percent", overrides=DIRC),
    timeseries("Lost messages per group (GE1 to GE9)", "Which multicast groups lose messages.", [target(f'pp_msgs_lost{{direction="{D1}"}}', "{{group}}")], pos(0, 40, 12, 7), unit="short"),
    timeseries("Lost messages per group (GE9 to GE1)", "The echo path.", [target(f'pp_msgs_lost{{direction="{D2}"}}', "{{group}}")], pos(12, 40, 12, 7), unit="short"),

    row("Per multicast group", 47),
    table("GE1 to GE9: per group", pos(0, 48, 12, 12),
          [("A", f'sum by(group) (pp_msgs_sent{{direction="{D1}"}})'), ("B", f'sum by(group) (pp_msgs_received{{direction="{D1}"}})'), ("C", f'sum by(group) (pp_msgs_lost{{direction="{D1}"}})')]),
    table("GE9 to GE1 (echo): per group", pos(12, 48, 12, 12),
          [("A", f'sum by(group) (pp_msgs_sent{{direction="{D2}"}})'), ("B", f'sum by(group) (pp_msgs_received{{direction="{D2}"}})'), ("C", f'sum by(group) (pp_msgs_lost{{direction="{D2}"}})')]),
]

LOKI = {"type": "loki", "uid": "loki"}


def logs(title, desc, expr, grid):
    return {"id": nxt(), "type": "logs", "title": title, "description": desc, "datasource": LOKI, "gridPos": grid,
            "targets": [{"datasource": LOKI, "expr": expr, "refId": "A", "queryType": "range", "maxLines": 60}],
            "options": {"showTime": False, "showLabels": False, "showCommonLabels": False, "wrapLogMessage": False, "prettifyLogMessage": False,
                        "enableLogDetails": False, "dedupStrategy": "none", "sortOrder": "Descending"}}


# live feed goes right under the message counters: shift everything from y=29 down by 18 rows
for p_ in panels:
    if p_["gridPos"]["y"] >= 29:
        p_["gridPos"]["y"] += 18
panels += [
    row("Live message feed (sampled: every group about once per 2 s per side; losses are always shown)", 29),
    logs("GE1 side: sent (TX) and received (RX)", "TX = GE1 sends the mix, RX = reply echoed by GE9. Newest first.",
         '{job="pingpong",role="A",kind=~"TX|RX"}', pos(0, 30, 12, 11)),
    logs("GE9 side: received (RX) and echoed (TX)", "RX = mix arriving from GE1, TX = echo sent back. Newest first.",
         '{job="pingpong",role="B",kind=~"TX|RX"}', pos(12, 30, 12, 11)),
    logs("Lost / bad / wrong-DSCP messages (every event)", "Empty means nothing was lost. LOSS = sequence gap seen at the receiver, BAD = checksum/format error, DSCP = arrived with another priority class.",
         '{job="pingpong",kind=~"LOSS|BAD|DSCP"}', pos(0, 41, 24, 1)),
]
panels[-1]["gridPos"]["h"] = 5

dash = {
    "uid": "pingpong-450", "title": "IEC 61162-450 Ping-Pong Test (GE1 <-> GE9)", "tags": ["snmp", "450", "cbs350"], "schemaVersion": 39, "version": 1,
    "editable": True, "refresh": "5s", "time": {"from": "now-15m", "to": "now"}, "timezone": "browser", "graphTooltip": 1,
    "annotations": {"list": [{"datasource": DS, "enable": True, "name": "Test running", "iconColor": "rgba(130,130,130,0.35)", "expr": "pp_run_active == 1",
                              "step": "5s", "titleFormat": "Test running", "useValueForTime": False}]},
    "panels": panels, "templating": {"list": []},
}
if __name__ == "__main__":
    out = os.path.join(ROOT, "config/grafana/dashboards/pingpong450.json")
    json.dump(dash, open(out, "w"), indent=1)
    print(out, len(panels), "panels")
