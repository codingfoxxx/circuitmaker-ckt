#!/usr/bin/env python3
"""Static checks for a CircuitMaker 2000 .CKT (no GUI needed).

    python validate.py file.CKT [--quiet]

Reports, using the same connectivity rules as CircuitMaker (wire metadata):
  * netlist with pin names (REF.PIN)
  * ERROR  floating digital inputs, nets driven by 2+ outputs, nets with one pin
  * ERROR  wire ends whose drawing does not touch the pin/wire they are connected to
  * WARN   drawing lies: a wire passing over a pin tip it is NOT connected to,
           collinear overlapping segments of different nets, non-orthogonal segments
  * WARN   74LS90/92/93 preset (MS1/MS2) not tied low, reset pins left open
Exit code 1 if any ERROR.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict

import ckt
import devlib


def io_classes(comp) -> tuple[set[int], set[int]]:
    """(inputs, outputs) as 1-based pin indexes, from the SPICE/XSPICE template."""
    sp = comp.spice or ""
    m = re.search(r"\[([^\]]*)\]\s*\+?\s*\[([^\]]*)\]", sp.replace("\r\n", " "))
    if not m:
        return set(), set()
    ins = {int(x) for x in re.findall(r"%(\d+)i\b", m.group(1))}
    outs = {int(x) for x in re.findall(r"%(\d+)o\b", m.group(2))} - ins
    return ins, outs


SOURCE_SYMBOLS = {"Logic Switch~": {1}, "Pulser~": {3, 4}, "+V": {1}, "Ground~": {1},
                  "Data Seq~": set(range(1, 9))}


def on_polyline(pts, p) -> bool:
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 == x1 == p[0] and min(y0, y1) <= p[1] <= max(y0, y1):
            return True
        if y0 == y1 == p[1] and min(x0, x1) <= p[0] <= max(x0, x1):
            return True
    return False


def check(path: str):
    c = ckt.Circuit.load(path)
    errors, warns = [], []
    syms = devlib.load()
    comps = c.components
    names = {}
    for i, comp in enumerate(comps, 1):
        s = syms.get(comp.name)
        if s:
            raw = [p.name or str(p.index) for p in s.pins]
            cnt, seen, nm = {}, {}, []
            for r in raw:
                cnt[r] = cnt.get(r, 0) + 1
            for r in raw:
                seen[r] = seen.get(r, 0) + 1
                nm.append(f"{r}{seen[r]}" if cnt[r] > 1 else r)
            names[i] = nm
        else:
            names[i] = None

    def pname(dev, pin):
        n = names.get(dev)
        nm = n[pin - 1] if n and 0 < pin <= len(n) else str(pin)
        return f"{c.ref(dev)}.{nm}"

    nets = ckt.build_nets(c)
    pin2net = {}
    for k, n in enumerate(nets):
        for dp in n:
            pin2net[dp] = k

    # ---- geometry vs metadata
    tips = {}
    for i, comp in enumerate(comps, 1):
        s = syms.get(comp.name)
        if not s:
            continue
        for p in s.pins:
            tx, ty = devlib.rotate(*p.tip(), comp.rotation, comp.mirrored)
            tips[(i, p.index)] = (comp.x + tx, comp.y + ty)
    for wi, w in enumerate(c.wires, 1):
        for (x0, y0), (x1, y1) in zip(w.pts, w.pts[1:]):
            if x0 != x1 and y0 != y1:
                warns.append(f"wire {wi}: diagonal segment ({x0},{y0})-({x1},{y1})")
        for end, (dev, pin, wref) in enumerate(((w.devA, w.pinA, w.wireA), (w.devB, w.pinB, w.wireB))):
            pt = w.pts[0] if end == 0 else w.pts[-1]
            if dev:
                comp = comps[dev - 1]
                t = tips.get((dev, pin))
                if t and pt != t and not (comp.rotation or comp.mirrored):
                    errors.append(f"wire {wi} end at {pt} is connected to {pname(dev, pin)} "
                                  f"but that pin tip is at {t} (drawing looks disconnected)")
            elif wref:
                if wref > len(c.wires):
                    errors.append(f"wire {wi} lands on wire {wref} which does not exist")
                elif not on_polyline(c.wires[wref - 1].pts, pt):
                    errors.append(f"wire {wi} end {pt} is joined to wire {wref} but does not touch it")
            else:
                warns.append(f"wire {wi} end {pt} is dangling (not connected to anything)")
    # wire passing over a foreign pin tip
    _, wire_net = ckt.build_nets(c, with_wires=True)
    for wi, w in enumerate(c.wires, 1):
        for (dev, pin), t in tips.items():
            if on_polyline(w.pts, t) and wire_net.get(wi) is not None and pin2net.get((dev, pin)) != wire_net.get(wi):
                warns.append(f"wire {wi} passes over pin tip of {pname(dev, pin)} without connecting "
                             f"(looks connected on screen)")
    # overlapping collinear segments of different nets
    segs = []
    for wi, w in enumerate(c.wires, 1):
        for a, b in zip(w.pts, w.pts[1:]):
            segs.append((wi, a, b))
    for i in range(len(segs)):
        for j in range(i + 1, len(segs)):
            wi, a, b = segs[i]
            wj, p, q = segs[j]
            if wi == wj or wire_net.get(wi) == wire_net.get(wj):
                continue
            if a[0] == b[0] == p[0] == q[0]:
                lo, hi = max(min(a[1], b[1]), min(p[1], q[1])), min(max(a[1], b[1]), max(p[1], q[1]))
                if hi > lo:
                    warns.append(f"wires {wi} and {wj} overlap on x={a[0]} y={lo}..{hi} (different nets)")
            elif a[1] == b[1] == p[1] == q[1]:
                lo, hi = max(min(a[0], b[0]), min(p[0], q[0])), min(max(a[0], b[0]), max(p[0], q[0]))
                if hi > lo:
                    warns.append(f"wires {wi} and {wj} overlap on y={a[1]} x={lo}..{hi} (different nets)")

    # wires crossing a component body (unrotated parts only)
    for i, comp in enumerate(comps, 1):
        s = syms.get(comp.name)
        if not s or comp.rotation or comp.mirrored or len(s.pins) < 2:
            continue
        bx0, by0, bx1, by1 = s.body_box()
        bx0, by0, bx1, by1 = comp.x + bx0 + 1, comp.y + by0 + 4, comp.x + bx1 - 1, comp.y + by1 - 4
        for wi, w in enumerate(c.wires, 1):
            for (x0, y0), (x1, y1) in zip(w.pts, w.pts[1:]):
                if x0 == x1 and bx0 < x0 < bx1 and min(y0, y1) < by1 and max(y0, y1) > by0:
                    warns.append(f"wire {wi} crosses the body of {c.ref(i)}")
                    break
                if y0 == y1 and by0 < y0 < by1 and min(x0, x1) < bx1 and max(x0, x1) > bx0:
                    warns.append(f"wire {wi} crosses the body of {c.ref(i)}")
                    break
    # a corner of one net lying on another net's wire looks like a junction
    for wi, w in enumerate(c.wires, 1):
        for p in w.pts[1:-1]:
            for wj, v in enumerate(c.wires, 1):
                if wj != wi and wire_net.get(wi) is not None and wire_net.get(wi) != wire_net.get(wj) \
                        and on_polyline(v.pts, p):
                    warns.append(f"corner {p} of wire {wi} lies on wire {wj} of another net (looks like a junction)")

    # ---- electrical
    drivers = defaultdict(list)
    for i, comp in enumerate(comps, 1):
        ins, outs = io_classes(comp)
        if comp.name in SOURCE_SYMBOLS:
            outs = SOURCE_SYMBOLS[comp.name]
        for o in outs:
            if (i, o) in pin2net:
                # all Ground / +V symbols are one global source, count them once
                label = {"Ground~": "GND", "+V": "VCC"}.get(comp.name) or pname(i, o)
                if label not in drivers[pin2net[(i, o)]]:
                    drivers[pin2net[(i, o)]].append(label)
        if comp.name == "+V":
            m = re.search(r"[-+]?\d+(\.\d+)?", comp.label or "")
            v = float(m.group(0)) if m else None
            if v is None or not 2.0 <= v <= 5.5:
                errors.append(f"{c.ref(i)} is a {comp.label or '?'} supply, not a TTL logic 1 (+5V)")
        for pin in sorted(ins):
            if (i, pin) not in pin2net:
                errors.append(f"floating input {pname(i, pin)}")
        if comp.name in ("74LS90", "74LS92", "74LS93"):
            for pn in ("MR1", "MR2", "MS1", "MS2"):
                s = syms.get(comp.name)
                try:
                    p = s.pin(pn)
                except KeyError:
                    continue
                if (i, p.index) not in pin2net:
                    errors.append(f"{c.ref(i)}.{pn} unconnected (TTL floats HIGH: "
                                  f"{'counter held reset' if pn.startswith('MR') else 'preset-to-9 active'})")
    for k, d in drivers.items():
        if len(d) > 1:
            errors.append(f"net driven by several outputs: {', '.join(d)}")
    for n in nets:
        if len(n) == 1:
            (dev, pin), = n
            warns.append(f"net with a single pin: {pname(dev, pin)}")

    lines = [f"{path}: {len(comps)} components, {len(c.wires)} wires, {len(nets)} nets"]
    for k, n in enumerate(sorted(nets, key=lambda s: sorted(s)), 1):
        lines.append(f"  net{k}: " + ", ".join(pname(d, p) for d, p in sorted(n)))
    return errors, warns, lines


def main(argv):
    quiet = "--quiet" in argv
    files = [a for a in argv if not a.startswith("--")]
    rc = 0
    for f in files:
        errors, warns, lines = check(f)
        if not quiet:
            print("\n".join(lines))
        for e in errors:
            print("ERROR", e)
        for w in warns:
            print("WARN ", w)
        print(f"{f}: {len(errors)} errors, {len(warns)} warnings")
        rc |= 1 if errors else 0
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
