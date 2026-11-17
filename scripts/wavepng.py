#!/usr/bin/env python3
"""Draw a timing diagram (PNG) from per-tick probe traces.

The traces are a JSON object {"NAME": [0, 1, "X", ...], ...} with one sample per
tick, as printed by ``cm_verify.py wave F.CKT --json`` (CircuitMaker's own
simulation, one entry per SCOPE probe) or saved by ``CM.digital_trace``.

    python cm_verify.py wave F.CKT --ticks 600 --json > trace.json
    python wavepng.py trace.json waves.png --signals LOAD,CLK,RDY --title "8-bit link"

``cm_verify.py wave F.CKT waves.png`` does both steps at once.  X (unknown) is
drawn as a grey line at mid height.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLORS = [(0, 0, 160), (200, 0, 0), (0, 120, 0), (150, 0, 150), (0, 120, 140), (160, 90, 0)]


def _font(size: int):
    try:
        return ImageFont.truetype("arial.ttf", size)
    except OSError:
        return ImageFont.load_default()


def render(traces: dict, out, signals: list[str] | None = None, title: str = "") -> Path:
    names = [n for n in (signals or list(traces)) if n in traces]
    if not names:
        raise ValueError(f"no such signals; trace has {sorted(traces)}")
    n = max(len(traces[k]) for k in names)
    sx = max(0.5, min(6.0, 1400 / max(n, 1)))        # pixels per tick
    left, row, top = 70, 46, 40
    w, h = int(left + n * sx + 30), top + row * len(names) + 40
    im = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(im)
    f, fs = _font(13), _font(11)
    d.text((left, 10), title, fill="black", font=f)
    step = next((s for s in (5, 10, 20, 50, 100, 200, 500, 1000, 2000) if n / s <= 16), 5000)
    bottom = top + row * len(names)
    for t in range(0, n + 1, step):
        x = left + t * sx
        d.line([(x, top - 5), (x, bottom)], fill=(225, 225, 225))
        d.text((x - 8, bottom + 4), str(t), fill=(110, 110, 110), font=fs)
    d.text((w - 60, bottom + 18), "ticks", fill=(110, 110, 110), font=fs)
    for i, name in enumerate(names):
        lo = top + i * row + 32
        hi = lo - 22
        d.text((8, hi + 4), name, fill="black", font=f)
        color = COLORS[i % len(COLORS)]
        prev = None
        for t, v in enumerate(traces[name]):
            v = str(v)
            y = hi if v == "1" else lo if v == "0" else (lo + hi) / 2
            x0, x1 = left + t * sx, left + (t + 1) * sx
            if prev is not None and prev != y:
                d.line([(x0, prev), (x0, y)], fill=color, width=2)
            d.line([(x0, y), (x1, y)], fill=color if v in "01" else (150, 150, 150), width=2)
            prev = y
    out = Path(out)
    im.save(out)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("trace", help="JSON file {name: [samples]}")
    ap.add_argument("png", help="output PNG")
    ap.add_argument("--signals", default="", help="comma list, top to bottom (default: all)")
    ap.add_argument("--title", default="")
    a = ap.parse_args(argv)
    traces = json.loads(Path(a.trace).read_text(encoding="utf-8"))
    sig = [s.strip() for s in a.signals.split(",") if s.strip()] or None
    print(render(traces, a.png, sig, a.title or Path(a.trace).stem))


if __name__ == "__main__":
    main()
