"""Cuts a looping slice of the official radar spoke stream out of a pcapng capture -> data/radar_bundles.bin

    .venv/bin/python tools/extract_radar.py /home/sav1023/Code/test/radar_dump.pcapng [--bundles 2048]

The capture (HALO20 radar, 236.6.7.8:6678) holds 17160-byte UDP datagrams (8-byte outer header + 32 spokes of 536 bytes, ~32 datagrams/s,
~36 Mbit/s). They are IP-fragmented on the wire; the reader in /home/sav1023/Code/test/pcap_spoke_replay.py reassembles them.
The slice must loop without a seam: every datagram carries the first spoke's sequence number at payload offset 10 (u16 little endian,
+32 per datagram, wraps at 4096 = one antenna revolution = 128 datagrams), so a slice of a multiple of 128 datagrams that is gap-free
continues seamlessly when replayed over and over. pp.py uses that field as the in-band counter for the radar stream.
File format: b"RADR" | u32 count | f64 datagrams-per-second | count x (u32 length | payload). Stays out of git (data/ is ignored).
"""
import argparse
import os
import socket
import struct
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "data", "radar_bundles.bin")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap")
    ap.add_argument("--bundles", type=int, default=2048, help="datagrams in the slice (multiple of 128 = whole antenna revolutions)")
    ap.add_argument("--src-ip", default="236.6.7.8")
    ap.add_argument("--src-port", type=int, default=6678)
    ap.add_argument("--reader", default="/home/sav1023/Code/test", help="folder holding pcap_spoke_replay.py")
    a = ap.parse_args()
    if a.bundles % 128:
        sys.exit("--bundles must be a multiple of 128")
    sys.path.insert(0, a.reader)
    import pcap_spoke_replay as r

    run, prev = [], None            # current gap-free run of (ts, payload)
    for ts, _ip, _port, p in r.scan(a.pcap, socket.inet_aton(a.src_ip), a.src_port):
        ok = len(p) == 17160
        seq = struct.unpack_from("<H", p, 10)[0] if ok else None
        if ok and (prev is None or (seq - prev) % 4096 == 32):     # next datagram continues the counter (+32, wrap at 4096)
            run.append((ts, p))
        else:
            run = [(ts, p)] if ok else []                          # gap or odd-sized datagram: start a new run
        prev = seq
        if len(run) >= a.bundles + 128:
            break
    if len(run) < a.bundles:
        sys.exit(f"only {len(run)} gap-free datagrams found, need {a.bundles}")
    # start the slice at the first datagram whose counter is 0 mod 4096 only if present; otherwise any start works because we
    # take a whole number of revolutions (a.bundles is a multiple of 128 and each step is 32, so first and next-after-last counters match)
    sl = run[:a.bundles]
    hz = (len(sl) - 1) / (sl[-1][0] - sl[0][0])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as f:
        f.write(b"RADR" + struct.pack("<Id", len(sl), hz))
        for _ts, p in sl:
            f.write(struct.pack("<I", len(p)) + p)
    size = os.path.getsize(OUT)
    print(f"{len(sl)} datagrams, {hz:.2f}/s, {size / 1e6:.1f} MB -> {OUT}   (~{hz * 17160 * 8 / 1e6:.1f} Mbit/s payload per direction)")


if __name__ == "__main__":
    main()
