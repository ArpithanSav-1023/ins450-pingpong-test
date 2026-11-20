"""Which sentences the sender really sends: chosen from the list on the sender dashboard (served by tools/selector.py), stored in data/selection.json.

    {"disabled": ["NAVD/SI0001/GPGGA", "RADAR/stream", ...]}      (empty / missing file = everything is sent)
Keys: NMEA 0183 -> <GROUP>/<sensor SFI>/<sentence id>, binary -> <GROUP>/binary, syslog -> <GROUP>/syslog, the radar stream -> RADAR/stream.
pp.py (role A) re-reads the file when it changes, so a change applies to a running test within about a second. The sequence counters are
stamped AFTER the filter, so leaving a sentence out never shows up as loss.
"""
import json
import os
import re

from . import common as c
from . import sender450 as s450

PATH = os.path.join(c.PROJECT_ROOT, "data", "selection.json")
NM = re.compile(rb"^UdPbC\x00\\([^*\\]*)\*[0-9A-Fa-f]{2}\\[$!]?([A-Z0-9]{3,6}),")
RADAR_KEY = "RADAR/stream"


def key(group, raw):
    m = NM.match(raw)
    if m:
        sfi = next((x[2:] for x in m.group(1).decode().split(",") if x.startswith("s:")), "?")
        return f"{group}/{sfi}/{m.group(2).decode()}"
    if raw.startswith(b"<"):
        return f"{group}/syslog"
    return f"{group}/binary"


def catalog(groups):
    """[(key, group, label)] in sheet order: one entry per sensor and sentence type of one round of every group, plus the radar stream."""
    b, seen, out = s450.Builder(False), set(), []
    for g in groups:
        for p in s450.payloads(g, b):
            k = key(g, p)
            if k not in seen:
                seen.add(k)
                out.append((k, g, k.split("/", 1)[1].replace("/", "  ")))
    out.append((RADAR_KEY, "RADAR", "official radar spoke stream (17160 B datagrams)"))
    return out


class Selector:
    def __init__(self, path=PATH):
        self.path, self.mtime, self.disabled = path, -1.0, frozenset()

    def refresh(self):
        """Re-reads the file if it changed. Returns True when the selection changed."""
        try:
            mt = os.path.getmtime(self.path)
        except OSError:
            mt = 0.0
        if mt == self.mtime:
            return False
        self.mtime = mt
        try:
            new = frozenset(json.load(open(self.path)).get("disabled", []))
        except (OSError, ValueError):
            new = frozenset()
        changed = new != self.disabled
        self.disabled = new
        return changed

    def enabled(self, k):
        return k not in self.disabled


def save(disabled, path=PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump({"disabled": sorted(disabled)}, f)
    os.replace(path + ".tmp", path)
