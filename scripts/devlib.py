#!/usr/bin/env python3
"""Read pin geometry and names for CircuitMaker 2000 symbols from ``Device.lib``.

``Device.lib`` is a binary dump of CircuitMaker's in-memory library.  We do not
need all of it: only, for every symbol, its name (the string stored as the
component *name* in a ``.CKT``, e.g. ``74LS90``, ``4011~``, ``Logic Switch~``)
and its pin table.  Pin records look like::

    int32 x, int32 y, uint16 flags, uint32 ptr, int32 side, uint16 0,
    int32 namelen, char name[namelen]

``side`` is the ASCII code of l/r/t/b (pin sticks out to the left, right,
top, bottom).  ``flags`` 43 marks an inversion bubble (e.g. 74LS90 CP0/CP1,
NAND outputs).  (x, y) is where the pin meets the body; the wire attaches at
the pin tip, ``PIN_LEN`` further out in the ``side`` direction (verified
against every pin-terminated wire in the shipped examples, see
``references/ckt-format.md``).

The symbol name sits 266 bytes before the first pin record and its numeric id
12 bytes before the name.  Pin order in the table == pin index used by the
``.CKT`` wire records (1-based) == ``%1..%n`` order of the SPICE template.

CLI:  python devlib.py show 74LS90        python devlib.py find 7490
"""
from __future__ import annotations

import json
import os
import re
import struct
import sys
from dataclasses import dataclass, asdict
from functools import lru_cache
from pathlib import Path

CM_DIR = Path(os.environ.get("CM2000_DIR", r"C:\Program Files\CircuitMaker 2000"))
PIN_LEN = 6
BUBBLE_LEN = 6
SIDE_VEC = {"l": (-1, 0), "r": (1, 0), "u": (0, -1), "d": (0, 1)}

_PIN_RE = re.compile(rb"(?s)(.{4})(.{4})(.{2})(.{4})([a-z]\x00\x00\x00)(\x00\x00)([\x00-\x1f]\x00\x00\x00)")


@dataclass
class Pin:
    index: int      # 1-based
    name: str
    x: int
    y: int
    side: str
    flags: int

    @property
    def inverted(self) -> bool:
        return self.flags == 43

    def tip(self) -> tuple[int, int]:
        """Wire attach point relative to the symbol origin (unrotated).

        Pin line is 6 px long; an inversion bubble adds another 6 px.
        """
        dx, dy = SIDE_VEC.get(self.side, (0, 0))
        L = PIN_LEN + (BUBBLE_LEN if self.inverted else 0)
        return self.x + dx * L, self.y + dy * L


@dataclass
class Symbol:
    name: str
    symid: int
    offset: int
    pins: list[Pin]
    devices: list = None   # default device records found after the pin table

    def body_box(self) -> tuple[int, int, int, int]:
        """Approximate body rectangle from pin bases (x0, y0, x1, y1)."""
        xs = [p.x for p in self.pins] or [0]
        ys = [p.y for p in self.pins] or [0]
        return min(xs), min(ys) - 9, max(xs), max(ys) + 9

    def pin(self, key: str | int) -> Pin:
        if isinstance(key, int):
            return self.pins[key - 1]
        hits = [p for p in self.pins if p.name == key]
        if not hits:
            # duplicate names (gate inputs are all "I"): accept I1, I2, ... by order
            m = re.fullmatch(r"(.*?)(\d+)", key)
            if m:
                same = [p for p in self.pins if p.name == m.group(1)]
                k = int(m.group(2))
                if len(same) > 1 and 1 <= k <= len(same):
                    return same[k - 1]
        if len(hits) != 1:
            raise KeyError(f"{self.name}: pin {key!r} matches {len(hits)} pins "
                           f"({[p.name for p in self.pins]})")
        return hits[0]


def _printable(b: bytes) -> bool:
    return all(32 <= c < 127 for c in b)


@lru_cache(maxsize=4)
def load(path: str | None = None) -> dict[str, Symbol]:
    data = Path(path or CM_DIR / "Device.lib").read_bytes()
    recs = []
    for m in _PIN_RE.finditer(data):
        n = data[m.end() - 4]
        name = data[m.end():m.end() + n]
        if not _printable(name):
            continue
        x, y = struct.unpack("<ii", data[m.start():m.start() + 8])
        if abs(x) > 500 or abs(y) > 500:
            continue
        flags = struct.unpack("<H", m.group(3))[0]
        recs.append((m.start(), m.end() + n, x, y, chr(data[m.start(5)]), flags, name.decode()))
    tables, cur = [], []
    for r in recs:
        if cur and r[0] != cur[-1][1]:
            tables.append(cur)
            cur = []
        cur.append(r)
    if cur:
        tables.append(cur)
    symbols: dict[str, Symbol] = {}
    ordered = []
    for t in tables:
        start = t[0][0]
        raw = data[start - 266:start - 246].split(b"\0")[0]
        if not raw or not _printable(raw):
            continue
        name = raw.decode()
        # the fixed-offset heuristic occasionally lands inside a previous
        # device's strings ("digital", "a$"); those are not symbol names
        if len(name) < 2 or name in ("digital", "source", "analog") or "$" in name:
            continue
        symid = struct.unpack("<i", data[start - 278:start - 274])[0]
        pins = [Pin(i + 1, r[6], r[2], r[3], r[4], r[5]) for i, r in enumerate(t) if r[4] != "n"]
        sym = Symbol(name, symid, start, pins)
        ordered.append(sym)
        if name not in symbols:          # first definition wins (later ones are aliases)
            symbols[name] = sym
    # default device record(s) stored after each symbol's pin table
    for i, sym in enumerate(ordered):
        end = ordered[i + 1].offset - 300 if i + 1 < len(ordered) else len(data)
        sym.devices = _device_records(data[sym.offset:max(end, sym.offset)])
    return symbols


_STR_RE = re.compile(rb"[\x20-\x7e\r\n]{2,}(?=\x00)")


def _device_records(seg: bytes) -> list[dict]:
    """Find (pinattr, spice, model, type, package) groups in a symbol's segment."""
    strs = [m.group().decode("latin-1") for m in _STR_RE.finditer(seg)]
    out = []
    for i, s in enumerate(strs):
        if "%D" not in s and not s.startswith("*"):
            continue
        rec = {"spice": s, "pinattr": "", "model": "", "type": "", "package": ""}
        if i > 0 and re.fullmatch(r"([A-Z]+=\d+;)+|[A-Z]+;", strs[i - 1]):
            rec["pinattr"] = strs[i - 1]
        rest = strs[i + 1:i + 4]
        if rest:
            rec["model"] = rest[0]
        for r in rest[1:]:
            if r.startswith(("type:", "alias:")) and not rec["type"]:
                rec["type"] = r
            elif re.fullmatch(r"(DIP|SIP|TO|AXIAL|RAD|DB|LCC|PLCC|SOT|QFP|SO)[\w.\-]*", r) and not rec["package"]:
                rec["package"] = r
        out.append(rec)
    return out


# ------------------------------------------------- model name -> symbol/pinout
def _first_existing(*paths):
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return None


def _virtualstore() -> Path:
    la = os.environ.get("LOCALAPPDATA", "")
    return Path(la) / "VirtualStore" / str(CM_DIR).split(":", 1)[-1].lstrip("\\/")


@lru_cache(maxsize=1)
def devicedb() -> dict[str, str]:
    """Tree device name (e.g. '7432') -> symbol name (e.g. '2-In OR')."""
    p = _first_existing(CM_DIR / "Devicedb.dat")
    out = {}
    if p:
        for ln in p.read_text("latin-1").splitlines():
            if ln.startswith("3") and "|" in ln:
                name, sym = ln[1:].split("|", 1)
                if name:
                    out.setdefault(name, sym)
    return out


@lru_cache(maxsize=1)
def searchdb() -> list[list[str]]:
    """Rows: [category, subcategory, tree device name, model, model comment]."""
    p = _first_existing(_virtualstore() / "SEARCHDB.DAT", CM_DIR / "SEARCHDB.DAT")
    rows = []
    if p:
        for ln in p.read_text("latin-1").splitlines()[1:]:
            parts = ln.split("\t")
            if len(parts) >= 3:
                rows.append((parts + ["", ""])[:5])
    return rows


def parse_mod_comment(comment: str) -> dict:
    """'*LS Quad 2-in OR - type:digital pkg:DIP14 [DVCC=14;DGND=7;](A:1,2,3)(B:4,5,6)'"""
    out = {"type": "", "package": "", "pinattr": "", "parts": []}
    m = re.search(r"type:(\w+)", comment)
    if m:
        out["type"] = "type:" + m.group(1)
    m = re.search(r"pkg:(\S+)", comment)
    if m:
        out["package"] = m.group(1)
    m = re.search(r"\[([^\]]*)\]", comment)
    if m:
        out["pinattr"] = m.group(1)
    for m in re.finditer(r"\(([A-Z]:)?([\d ,]+)\)", comment):
        pins = [int(x) for x in re.split(r"[ ,]+", m.group(2).strip()) if x]
        out["parts"].append(pins)
    return out


@lru_cache(maxsize=1)
def mod_index() -> dict[str, str]:
    """MODEL name (without the leading 'A') -> its '*comment' line, from Models/*.MOD."""
    out = {}
    for p in sorted((CM_DIR / "Models").glob("*")):
        if p.suffix.upper() not in (".MOD", ".SUB"):
            continue
        last_comment = ""
        for ln in p.read_text("latin-1", errors="replace").splitlines():
            if ln.startswith("*"):
                last_comment = ln
            m = re.match(r"\.MODEL\s+A(\S+)", ln, re.I)
            if m:
                out.setdefault(m.group(1).upper(), last_comment)
    return out


def tree_name_for(model: str) -> str:
    """'74LS00' -> '7400', '74HC32' -> '7432', '4011' -> '4011'."""
    m = re.fullmatch(r"74(?:LS|HCT|HC|ALS|AS|F|S|L|H|C|ACT|AC)?(\d+)", model.upper())
    return f"74{m.group(1)}" if m else model


def resolve_model(model: str) -> dict:
    """Resolve '74LS32' / '7432' / '4011' / '74LS90' to symbol + package data.

    Returns dict(symbol=<Symbol>, model=..., comment=..., mod=parse_mod_comment(...)).
    """
    syms = load()
    rows = searchdb()
    hit = None
    for r in rows:                        # exact model match first
        if r[3].upper() == model.upper():
            hit = r
            break
    if hit is None:
        for r in rows:
            if r[2].upper() == model.upper() and r[3]:
                hit = r
                break
    tree_name = hit[2] if hit else tree_name_for(model)
    model_name = hit[3] if hit else model
    comment = hit[4] if hit else mod_index().get(model.upper(), "")
    candidates = [devicedb().get(tree_name), devicedb().get(tree_name_for(model)),
                  tree_name, model, model_name]
    sym = None
    for c in candidates:
        if not c:
            continue
        for nm in (c + "~", c):
            if nm in syms:
                sym = syms[nm]
                break
        if sym:
            break
    if sym is None:
        raise KeyError(f"cannot resolve model {model!r} to a Device.lib symbol")
    return {"symbol": sym, "model": model_name, "comment": comment, "mod": parse_mod_comment(comment)}


def symbol(name: str) -> Symbol:
    syms = load()
    if name in syms:
        return syms[name]
    raise KeyError(f"symbol {name!r} not found in Device.lib")


def pin_names_for(component) -> list[str] | None:
    """Hook used by ckt.describe(): component -> ordered pin names."""
    try:
        raw = [p.name or str(p.index) for p in symbol(component.name).pins]
    except KeyError:
        return None
    cnt, seen, out = {}, {}, []
    for r in raw:
        cnt[r] = cnt.get(r, 0) + 1
    for r in raw:                       # duplicate names -> I1, I2 (same as build.py)
        seen[r] = seen.get(r, 0) + 1
        out.append(f"{r}{seen[r]}" if cnt[r] > 1 else r)
    return out


def rotate(x: int, y: int, rotation: int, mirror: int = 0) -> tuple[int, int]:
    """Apply CM rotation (degrees, counter-clockwise on screen) to a symbol offset.

    Screen y grows downwards.  Verified on the shipped examples for 0/90/180/270.
    """
    if mirror:
        x = -x
    r = rotation % 360
    if r == 0:
        return x, y
    if r == 90:
        return y, -x
    if r == 180:
        return -x, -y
    if r == 270:
        return -y, x
    raise ValueError(rotation)


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    cmd = argv[0]
    syms = load()
    if cmd == "show":
        for nm in argv[1:]:
            s = syms.get(nm) or syms.get(nm + "~")
            if not s:
                print(f"{nm}: not found")
                continue
            print(f"{s.name}  symid={s.symid}")
            for p in s.pins:
                print(f"  {p.index:2d} {p.name:6} base=({p.x},{p.y}) side={p.side} tip={p.tip()}"
                      f"{' (bubble)' if p.inverted else ''}")
    elif cmd == "find":
        pat = re.compile(argv[1], re.I)
        for nm in sorted(syms):
            if pat.search(nm):
                print(nm, len(syms[nm].pins), "pins")
    elif cmd == "json":
        print(json.dumps({k: [asdict(p) for p in v.pins] for k, v in syms.items()}, indent=0))
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
