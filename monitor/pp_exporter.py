"""Prometheus exporter for the ping-pong test: reads runs/<newest>/stats_A.json + stats_B.json (written every second by traffic.pp).

    .venv/bin/python monitor/pp_exporter.py        # serves :9103/metrics
direction label: ge1_to_ge9 = A sends / B receives;  ge9_to_ge1 = B sends (echo + filler) / A receives.
All counters are cumulative since the run started (exported as gauges). message = one 450 sample, packet = any UDP datagram (mix+filler),
data = payload bytes. Loss is counted from the in-band sequence numbers (gaps), so it is live and exact except for the very last packets
of a run (the final summary.json uses sender-minus-receiver and covers those too).
"""
import argparse
import glob
import json
import os
import time

from prometheus_client import Gauge, start_http_server

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILL_WIRE = 1446
FILL_PAYLOAD = 1400


def newest_run(runs):
    c = [d for d in glob.glob(os.path.join(runs, "*")) + glob.glob(os.path.join(runs, "*", "*")) if os.path.exists(os.path.join(d, "stats_A.json")) or os.path.exists(os.path.join(d, "stats_B.json"))]
    # newest = most recently written stats file (a side running on its own has only its own file)
    mt = lambda d: max(os.path.getmtime(os.path.join(d, f)) for f in ("stats_A.json", "stats_B.json") if os.path.exists(os.path.join(d, f)))
    return max(c, key=mt) if c else None


def load(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9103)
    ap.add_argument("--runs", default=os.path.join(ROOT, "runs"))
    ap.add_argument("--phase-file", default=os.path.join(ROOT, "data", "phase.txt"))
    a = ap.parse_args()
    D, G = ["direction"], ["direction", "group"]
    g = {
        "sent": Gauge("pp_msgs_sent", "450 messages sent (mix)", G), "recv": Gauge("pp_msgs_received", "450 messages received", G),
        "lost": Gauge("pp_msgs_lost", "450 messages lost (sequence gaps)", G), "bad": Gauge("pp_msgs_bad", "messages received with bad checksum/format", G),
        "wrong": Gauge("pp_msgs_wrong_dscp", "messages received with the wrong DSCP", G),
        "sbytes": Gauge("pp_msg_bytes_sent", "mix payload bytes sent", G), "rbytes": Gauge("pp_msg_bytes_received", "mix payload bytes received", G),
        "fs": Gauge("pp_filler_pkts_sent", "filler packets sent", D), "fr": Gauge("pp_filler_pkts_received", "filler packets received", D),
        "fl": Gauge("pp_filler_pkts_lost", "filler packets lost (sequence gaps)", D),
        "fsb": Gauge("pp_filler_bytes_sent", "filler payload bytes sent", D), "frb": Gauge("pp_filler_bytes_received", "filler payload bytes received", D),
        "p_sent": Gauge("pp_packets_sent", "all UDP packets sent (mix + filler)", D), "p_recv": Gauge("pp_packets_received", "all UDP packets received", D),
        "p_lost": Gauge("pp_packets_lost", "all UDP packets lost", D), "p_loss": Gauge("pp_packet_loss_percent", "packet loss %", D),
        "m_sent": Gauge("pp_messages_sent", "all messages sent", D), "m_recv": Gauge("pp_messages_received", "all messages received", D),
        "m_lost": Gauge("pp_messages_lost", "all messages lost", D), "m_loss": Gauge("pp_message_loss_percent", "message loss %", D),
        "d_sent": Gauge("pp_data_bytes_sent", "payload bytes sent (mix + filler)", D), "d_recv": Gauge("pp_data_bytes_received", "payload bytes received", D),
        "d_lost": Gauge("pp_data_bytes_lost", "payload bytes lost (estimate from gaps)", D), "d_loss": Gauge("pp_data_loss_percent", "data loss %", D),
    }
    # per-side metrics: each side (role A = sender PC, role B = receiver/echo PC) exported on its own, from its OWN stats file only,
    # so a side can be shown (and run) individually. role A/B; "received" = what that side's receiver counted.
    R, RG = ["role"], ["role", "group"]
    sd = {
        "sent": Gauge("pp_side_msgs_sent", "messages this side sent", RG), "recv": Gauge("pp_side_msgs_received", "messages this side received", RG),
        "lost": Gauge("pp_side_msgs_lost", "messages this side saw missing (sequence gaps)", RG), "bad": Gauge("pp_side_msgs_bad", "bad messages received", RG),
        "wrong": Gauge("pp_side_msgs_wrong_dscp", "messages received with the wrong DSCP", RG),
        "sbytes": Gauge("pp_side_msg_bytes_sent", "mix payload bytes sent", RG), "rbytes": Gauge("pp_side_msg_bytes_received", "mix payload bytes received", RG),
        "fs": Gauge("pp_side_filler_pkts_sent", "ONF dump packets sent", R), "fr": Gauge("pp_side_filler_pkts_received", "ONF dump packets received", R),
        "fl": Gauge("pp_side_filler_pkts_lost", "ONF dump packets this side saw missing", R),
        "ps": Gauge("pp_side_packets_sent", "all packets sent (mix + ONF)", R), "pr": Gauge("pp_side_packets_received", "all packets received", R),
        "pl": Gauge("pp_side_packets_lost", "all packets seen missing", R),
        "ms": Gauge("pp_side_messages_sent", "all messages sent", R), "mr": Gauge("pp_side_messages_received", "all messages received", R),
        "ml": Gauge("pp_side_messages_lost", "all messages seen missing", R),
        "active": Gauge("pp_side_active", "1 while this side is running", R), "elapsed": Gauge("pp_side_sent_seconds", "seconds this side has been sending", R),
    }
    st_g = {k: Gauge(f"pp_side_stream_{k}", d, ["role", "stream"]) for k, d in (("pkts_sent", "stream packets sent"), ("pkts_received", "stream packets received"),
                                                                              ("pkts_lost", "stream packets this side saw missing"), ("target_mbps", "stream target Mbit/s"))}
    side_info = Gauge("pp_side_info", "run followed by this side", ["role", "run", "scenario"])
    # which switch port THIS PC is plugged into (the 100 Mbit/s USB NIC = GE9, otherwise GE1), so dashboards pick the right port by themselves
    try:        # per-PC setting file (data/pp_local.env: PP_PORT=GE9)
        for line in open(os.path.join(ROOT, "data", "pp_local.env")):
            if line.startswith("PP_PORT=") and not os.environ.get("PP_PORT"):
                os.environ["PP_PORT"] = line.split("=", 1)[1].strip().strip("\"'")
    except OSError:
        pass
    port = os.environ.get("PP_PORT") or ("GE9" if os.path.exists("/sys/class/net/enx207bd51e1472") else "GE1")
    Gauge("pp_this_pc_port", "switch port this PC is connected to", ["port", "name"]).labels("gi" + port[2:], port).set(1)
    target_g = Gauge("pp_target_mbps", "offered wire rate per direction (mix + filler)")
    active_g = Gauge("pp_run_active", "1 while a test is running")
    info_g = Gauge("pp_run_info", "newest run", ["run", "scenario"])
    phase_g = Gauge("pp_phase_info", "phase label", ["phase"])
    start_http_server(a.port, addr="127.0.0.1")
    print(f"[pp_exporter] :{a.port} following newest run in {a.runs}", flush=True)
    while True:
        try:
            phase = open(a.phase_file).readline().strip() or "unlabeled"
        except OSError:
            phase = "unlabeled"
        phase_g.clear(); phase_g.labels(phase).set(1)
        run = newest_run(a.runs)
        S = {r: load(os.path.join(run, f"stats_{r}.json")) for r in "AB"} if run else {}
        if run and S.get("A") and S.get("B"):
            fresh = max(S["A"]["ts"], S["B"]["ts"])
            active_g.set(1 if time.time() - fresh < 4 and not (S["A"]["final"] and S["B"]["final"]) else 0)
            info_g.clear(); info_g.labels(os.path.basename(run), S["A"]["scenario"]).set(1)
            target_g.set(S["A"]["target_mbps"])
            for gg in g.values():
                gg.clear()
            for d, (s, r) in {"ge1_to_ge9": (S["A"], S["B"]), "ge9_to_ge1": (S["B"], S["A"])}.items():
                ms = mr = ml = mb = 0
                sb = rb = lb = 0
                for grp, t in s["tx"].items():
                    rx = r["rx"][grp]
                    L = (d, grp)
                    g["sent"].labels(*L).set(t["msgs"]); g["recv"].labels(*L).set(rx["msgs"]); g["lost"].labels(*L).set(rx["lost"])
                    g["bad"].labels(*L).set(rx["bad"]); g["wrong"].labels(*L).set(rx["wrong_dscp"])
                    g["sbytes"].labels(*L).set(t["bytes"]); g["rbytes"].labels(*L).set(rx["bytes"])
                    ms += t["msgs"]; mr += rx["msgs"]; ml += rx["lost"]
                    sb += t["bytes"]; rb += rx["bytes"]
                    lb += rx["lost"] * (rx["bytes"] / rx["msgs"] if rx["msgs"] else 0)
                fs, fr = s["filler"], r["filler"]
                g["fs"].labels(d).set(fs["tx_pkts"]); g["fr"].labels(d).set(fr["rx_pkts"]); g["fl"].labels(d).set(fr["rx_lost"])
                g["fsb"].labels(d).set(fs["tx_bytes"]); g["frb"].labels(d).set(fr["rx_bytes"])
                ps, pr, pl = ms + fs["tx_pkts"], mr + fr["rx_pkts"], ml + fr["rx_lost"]
                g["p_sent"].labels(d).set(ps); g["p_recv"].labels(d).set(pr); g["p_lost"].labels(d).set(pl)
                g["p_loss"].labels(d).set(100 * pl / (pr + pl) if pr + pl else 0)
                g["m_sent"].labels(d).set(ms); g["m_recv"].labels(d).set(mr); g["m_lost"].labels(d).set(ml)
                g["m_loss"].labels(d).set(100 * ml / (mr + ml) if mr + ml else 0)
                dl = lb + fr["rx_lost"] * FILL_PAYLOAD
                g["d_sent"].labels(d).set(sb + fs["tx_bytes"]); g["d_recv"].labels(d).set(rb + fr["rx_bytes"]); g["d_lost"].labels(d).set(dl)
                tot = rb + fr["rx_bytes"] + dl
                g["d_loss"].labels(d).set(100 * dl / tot if tot else 0)
        else:
            active_g.set(0)
        for gg in list(sd.values()) + list(st_g.values()) + [side_info]:
            gg.clear()
        for role in "AB":
            st = S.get(role) if run else None
            if not st:
                continue
            ms = mr = ml = 0
            for grp, t in st["tx"].items():
                rx = st["rx"].get(grp, {"msgs": 0, "bytes": 0, "lost": 0, "bad": 0, "wrong_dscp": 0})
                L = (role, grp)
                sd["sent"].labels(*L).set(t["msgs"]); sd["recv"].labels(*L).set(rx["msgs"]); sd["lost"].labels(*L).set(rx["lost"])
                sd["bad"].labels(*L).set(rx["bad"]); sd["wrong"].labels(*L).set(rx["wrong_dscp"])
                sd["sbytes"].labels(*L).set(t["bytes"]); sd["rbytes"].labels(*L).set(rx["bytes"])
                ms += t["msgs"]; mr += rx["msgs"]; ml += rx["lost"]
            f = st["filler"]
            sd["fs"].labels(role).set(f["tx_pkts"]); sd["fr"].labels(role).set(f["rx_pkts"]); sd["fl"].labels(role).set(f["rx_lost"])
            sd["ps"].labels(role).set(ms + f["tx_pkts"]); sd["pr"].labels(role).set(mr + f["rx_pkts"]); sd["pl"].labels(role).set(ml + f["rx_lost"])
            sd["ms"].labels(role).set(ms); sd["mr"].labels(role).set(mr); sd["ml"].labels(role).set(ml)
            for n, x in (st.get("streams") or {}).items():
                st_g["pkts_sent"].labels(role, n).set(x["tx_pkts"]); st_g["pkts_received"].labels(role, n).set(x["rx_pkts"])
                st_g["pkts_lost"].labels(role, n).set(x["rx_lost"]); st_g["target_mbps"].labels(role, n).set(x["mbps"])
            sd["active"].labels(role).set(1 if time.time() - st["ts"] < 4 and not st["final"] else 0)
            sd["elapsed"].labels(role).set(st.get("sent_secs") or 0)
            side_info.labels(role, os.path.basename(run), st["scenario"]).set(1)
        time.sleep(1.0)


if __name__ == "__main__":
    main()
