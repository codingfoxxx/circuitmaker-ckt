#!/usr/bin/env python3
"""Decode CircuitMaker's digital waveform export (File > Export > Waveform
Metafile, an Aldus-placeable WMF) into per-probe sample lists.

Verified structure of the export: for every SCOPE probe the window draws the
probe name with TextOut at (13, base_y) and then one LineTo per tick from the
newest sample (right) to the oldest (left).  Each trace row has a baseline
``base_y`` (logic 0), ``base_y - 11`` (logic 1) and ``base_y - 5`` (unknown).
The horizontal step equals the X-magnification (pixels per tick).

    python wavewmf.py wave.wmf            # prints per-trace edges/periods

API: decode(path) -> {name: [v0, v1, ...]} oldest..newest, values 0/1/'X'.
"""
from __future__ import annotations

import struct
import sys
from collections import defaultdict


def _records(data: bytes):
    off = 22                     # placeable header
    hdr_words = struct.unpack("<H", data[off + 2:off + 4])[0]
    off += hdr_words * 2
    while off + 6 <= len(data):
        size, func = struct.unpack("<IH", data[off:off + 6])
        if size < 3:
            break
        yield func, data[off + 6:off + size * 2]
        if func == 0:
            break
        off += size * 2


def decode(path: str, xstep: int | None = None) -> dict[str, list]:
    data = open(path, "rb").read()
    labels = []                  # (y, name)
    pts = []                     # polyline points in drawing order, with breaks
    cur = None
    for func, p in _records(data):
        if func == 0x0521:       # TextOut
            n = struct.unpack("<h", p[:2])[0]
            s = p[2:2 + n].decode("latin-1")
            rest = p[2 + n + (n & 1):]
            y, x = struct.unpack("<hh", rest[:4])
            if x < 40:
                labels.append((y, s))
        elif func == 0x0214:     # MoveTo
            y, x = struct.unpack("<hh", p[:4])
            cur = [(x, y)]
            pts.append(cur)
        elif func == 0x0213:     # LineTo
            y, x = struct.unpack("<hh", p[:4])
            if cur is None:
                cur = [(x, y)]
                pts.append(cur)
            cur.append((x, y))
    out = {}
    for base_y, name in labels:
        # the trace polyline: longest run whose points all lie within the row
        cands = [pl for pl in pts if len(pl) > 4 and all(base_y - 12 <= y <= base_y for _, y in pl)
                 and min(x for x, _ in pl) >= 60]
        if not cands:
            continue
        pl = max(cands, key=len)
        # sample value at each distinct x (rightmost point = newest)
        level = {}
        for x, y in pl:
            v = 0 if y == base_y else 1 if y == base_y - 11 else "X"
            level.setdefault(x, v)          # first visit of an x = value of the segment ending there
        xs = sorted(level)
        if xstep is None:
            diffs = sorted({b - a for a, b in zip(xs, xs[1:]) if b > a})
            step = diffs[0] if diffs else 1
        else:
            step = xstep
        # value of tick k is the level of the horizontal segment between x and x+step
        samples = []
        seg = defaultdict(list)
        for (x0, y0), (x1, y1) in zip(pl, pl[1:]):
            if y0 == y1 and x0 != x1:
                v = 0 if y0 == base_y else 1 if y0 == base_y - 11 else "X"
                for xx in range(min(x0, x1), max(x0, x1), step):
                    seg[xx] = v
        for xx in sorted(seg):
            samples.append(seg[xx])
        out[name] = samples
    return out


def edges(samples, kind="rise"):
    res = []
    for i in range(1, len(samples)):
        a, b = samples[i - 1], samples[i]
        if kind == "rise" and a == 0 and b == 1:
            res.append(i)
        if kind == "fall" and a == 1 and b == 0:
            res.append(i)
    return res


def main(argv):
    w = decode(argv[0])
    for name, s in w.items():
        r = edges(s)
        per = sorted({b - a for a, b in zip(r, r[1:])})
        print(f"{name:8} {len(s)} ticks, rising edges={len(r)}, periods={per}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
