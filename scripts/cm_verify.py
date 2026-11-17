#!/usr/bin/env python3
"""Verify a .CKT inside CircuitMaker 2000 itself (Digital mode).

  python cm_verify.py nets   FILE.CKT              # CM opens it w/o popups + CM's nets == ours
  python cm_verify.py wave   FILE.CKT --ticks 600  # per-tick SCOPE probe traces (from WMF export)
  python cm_verify.py wave   FILE.CKT waves.png    # same, plus a timing-diagram PNG (wavepng.py)
  python cm_verify.py count  FILE.CKT --pulses 100 --clock CLK --displays DEZ,UNI
        [--click "60:RST,62:RST"]                  # read Hex Displays after every clock pulse
  python cm_verify.py shot   FILE.CKT out.png

Timing facts used (measured in CM): after Reset the Pulser output is LOW for
`low`+1 ticks, then alternates high/low; its k-th falling edge is at
t = 1 + k*(high+low).  One Step = 1 tick.  Never point this at files under
Program Files (VirtualStore redirection).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import ckt
import devlib
from cmgui import CM, capture, _enum_children


def nets_ours(c):
    out = []
    for n in ckt.build_nets(c):
        if len(n) > 1:
            out.append(sorted((c.components[d - 1].designator, p) for d, p in n))
    return sorted(out)


def nets_cm(c):
    g = defaultdict(list)
    for comp in c.components:
        try:
            npin = len(devlib.symbol(comp.name).pins)
        except KeyError:
            continue
        for pin, nid in enumerate(comp.nets[1:npin + 1], 1):
            if nid > 0:
                g[nid].append((comp.designator, pin))
    return sorted(sorted(v) for v in g.values() if len(v) > 1)


def cmd_nets(cm, f: Path):
    pops = cm.open(f)
    res = {"file": str(f), "popups": [(p["title"], p["texts"][:6]) for p in pops]}
    out = f.with_name(f.stem + "_cmresave" + f.suffix)
    cm.save_as(out)
    a, b = nets_ours(ckt.Circuit.load(f)), nets_cm(ckt.Circuit.load(out))
    res["nets_equal"] = a == b
    if a != b:
        res["only_ours"] = [x for x in a if x not in b]
        res["only_cm"] = [x for x in b if x not in a]
    out.unlink(missing_ok=True)
    return res


def cmd_count(cm, f: Path, pulses: int, clock: str, displays: list[str], clicks: dict):
    c = ckt.Circuit.load(f)
    pos = {comp.designator: (comp.x, comp.y) for comp in c.components}
    clk = c.comp(clock)
    period = clk.nets[7] + clk.nets[8]           # LOW + HIGH ticks
    cm.open(f)
    cm.digital_mode()
    cm.show_waveforms()
    cm.cmd("reset", wait=0.5)
    seq = []
    t = 0
    for k in range(0, pulses + 1):
        target = 1 + k * period + (period - 2)    # just before edge k+1
        cm.step_ticks(target - t)
        t = target
        d = cm.read_hex_displays({n: pos[n] for n in displays})
        seq.append("".join(d[n] for n in displays))
        for ref in clicks.get(k, []):
            cm.click_canvas(*pos[ref])
            time.sleep(0.3)
    return seq


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["nets", "wave", "count", "shot"])
    ap.add_argument("file")
    ap.add_argument("png", nargs="?")
    ap.add_argument("--ticks", type=int, default=600)
    ap.add_argument("--pulses", type=int, default=50)
    ap.add_argument("--clock", default="CLK")
    ap.add_argument("--displays", default="")
    ap.add_argument("--click", default="", help="'N:REF,...' click switch REF after pulse N")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    f = Path(a.file).resolve()
    if "program files" in str(f).lower():
        print("refusing: copy the file out of Program Files first (VirtualStore)")
        return 2
    if a.mode == "wave" and not any(x.name == "SCOPE" for x in ckt.Circuit.load(f).components):
        print(f"no SCOPE probe in {f.name}: add SCOPE parts on the signals to trace "
              "(build.py: s.add('SCOPE', 'P1', x, y, label='CLK') + s.branch('P1.1', wire, point))")
        return 2
    cm = CM.attach() if CM.running_pids() else CM.start()
    if a.mode == "nets":
        r = cmd_nets(cm, f)
        print(json.dumps(r, indent=1))
        return 0 if r["nets_equal"] and not r["popups"] else 1
    if a.mode == "shot":
        cm.open(f)
        time.sleep(2.0)                      # let CM finish repainting (wires are drawn last)
        # screen grab, not PrintWindow: PrintWindow returned half-drawn canvases
        im = cm.screen_canvas()
        c = ckt.Circuit.load(f)
        xs = [p[0] for w in c.wires for p in w.pts] + [x.x for x in c.components] + [n.rect[2] for n in c.notes]
        ys = [p[1] for w in c.wires for p in w.pts] + [x.y for x in c.components] + [n.rect[3] for n in c.notes]
        box = (0, 0, min(im.size[0], max(xs) + 60), min(im.size[1], max(ys) + 60))
        im.crop(box).save(a.png or f.with_suffix(".png"))
        return 0
    if a.mode == "wave":
        cm.open(f)
        tr = cm.digital_trace(a.ticks)
        if a.png:
            import wavepng
            print(wavepng.render(tr, a.png, title=f"{f.name} ({a.ticks} ticks)"), file=sys.stderr)
        if a.json:
            print(json.dumps(tr))
        else:
            for k, s in tr.items():
                print(f"{k:8} " + "".join(str(v) for v in s))
        return 0
    if a.mode == "count":
        clicks = defaultdict(list)
        for item in filter(None, a.click.split(",")):
            n, ref = item.split(":")
            clicks[int(n)].append(ref)
        seq = cmd_count(cm, f, a.pulses, a.clock, a.displays.split(","), clicks)
        print(f"CircuitMaker displays {a.displays} before pulse 1, then after each pulse:")
        for i in range(0, len(seq), 20):
            print(f"  [{i:3d}] " + " ".join(seq[i:i + 20]))
        return 0


if __name__ == "__main__":
    sys.exit(main())
