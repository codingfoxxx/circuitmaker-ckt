#!/usr/bin/env python3
"""Lossless reader/writer for CircuitMaker 2000 ``.CKT`` text schematics.

Format facts this module relies on (all verified against the 80+ example
files shipped with CircuitMaker 2000 and files re-saved by the program):

* The file is written by the C runtime in *text mode*: every ``\\n`` in memory
  became ``\\r\\n`` on disk.  Strings that themselves contain ``\\r\\n`` (SPICE
  templates, multi-line notes) therefore appear as ``\\r\\r\\n`` on disk.  We
  undo exactly that transform (``\\r\\n`` -> ``\\n``) and decode as latin-1, so
  ``write(parse(x)) == x`` byte for byte.
* Strings are length-prefixed: ``"<n> <n chars>"``; the empty string is ``"0"``.
  The length counts the in-memory form (``\\r\\n`` inside = 2 chars).
* Layout:  header | N components | W wires | T text notes | footer.
  Header and footer are kept verbatim (window/analysis settings).
* Component record (versions 5 / 5.5 / 5.6):
      name                       e.g. "74LS90", "Logic Switch~"
      symid x y mirror npins m   symbol id in Device.lib, origin, ...
      nets[0..npins]             net id per pin (index 0 unused); CM recomputes
      macro-name str
      a b flags rotation         rotation in degrees (0/90/180/270)
      label str [bbox]           value/label shown (bbox only if non-empty)
      designator str [bbox]      U1, S1 ...
      str, pinattr str, spice str, str, type str, package str
      k ; k+1 ints (pin map: [0, this part's pins..., all parts' pins..., junk])
      8 ints ; prefix str ; 3 ints ; c ; [c values] ; d
* Wire record: ``pinA pinB net 0 0 flags 0 devA devB wireA wireB npts`` then
  npts ``x y`` lines.  devX / pinX are 1-based; devX==0 means that end lands on
  another wire (wireX, 1-based) instead of a pin.  ``net`` and ``flags`` are
  recomputed by CircuitMaker on save.

Run ``python ckt.py --help`` for the CLI.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

INT_RE = re.compile(r"-?\d+")
NL = "\n"


class CktError(Exception):
    pass


# --------------------------------------------------------------------------- io
def decode_bytes(raw: bytes) -> str:
    return raw.replace(b"\r\n", b"\n").decode("latin-1")


def encode_text(text: str) -> bytes:
    return text.encode("latin-1").replace(b"\n", b"\r\n")


class Reader:
    def __init__(self, text: str, pos: int = 0):
        self.s = text
        self.i = pos

    def eof(self) -> bool:
        return self.i >= len(self.s)

    def line(self) -> str:
        j = self.s.find("\n", self.i)
        if j < 0:
            raise CktError(f"unexpected EOF at {self.i}")
        out = self.s[self.i:j]
        self.i = j + 1
        return out

    def peek_line(self) -> str:
        j = self.s.find("\n", self.i)
        return self.s[self.i:j] if j >= 0 else self.s[self.i:]

    def ints_line(self, count: int | None = None) -> list[int]:
        ln = self.line()
        parts = ln.split()
        try:
            vals = [int(p) for p in parts]
        except ValueError as e:
            raise CktError(f"expected ints, got {ln!r} at {self.i}") from e
        if count is not None and len(vals) != count:
            raise CktError(f"expected {count} ints, got {ln!r} at {self.i}")
        return vals

    def int_line(self) -> int:
        v = self.ints_line()
        if len(v) != 1:
            raise CktError(f"expected single int at {self.i}, got {v}")
        return v[0]

    def string(self) -> str:
        """Length-prefixed string; returns content (may contain newlines)."""
        start = self.i
        m = re.compile(r"(\d+)").match(self.s, self.i)
        if not m:
            raise CktError(f"expected string length at {self.i}: {self.peek_line()!r}")
        n = int(m.group(1))
        j = m.end()
        if n == 0:
            if self.s[j:j + 1] != "\n":
                raise CktError(f"bad empty string at {start}: {self.peek_line()!r}")
            self.i = j + 1
            return ""
        if self.s[j:j + 1] != " ":
            raise CktError(f"bad string at {start}: {self.peek_line()!r}")
        content = self.s[j + 1:j + 1 + n]
        if len(content) != n or self.s[j + 1 + n:j + 2 + n] != "\n":
            raise CktError(f"string length mismatch at {start}: {self.peek_line()!r}")
        self.i = j + 2 + n
        return content

    def int_block(self, count: int) -> list[int]:
        """Read ``count`` whitespace separated ints spanning lines; return ints."""
        vals: list[int] = []
        while len(vals) < count:
            ln = self.line()
            for p in ln.split():
                vals.append(int(p))
        if len(vals) != count:
            raise CktError(f"int block overflow: wanted {count} got {len(vals)}")
        return vals


def fmt_str(s: str) -> str:
    return f"{len(s)} {s}\n" if s else "0\n"


def fmt_ints(vals: Iterable[int]) -> str:
    return " ".join(str(v) for v in vals) + "\n"


def fmt_block10(vals: list[int], leading_newline: bool = False) -> str:
    """CM wraps int arrays at 10 per line."""
    out = "\n" if leading_newline else ""
    for i in range(0, len(vals), 10):
        out += " ".join(str(v) for v in vals[i:i + 10]) + "\n"
    return out


# ------------------------------------------------------------------ components
@dataclass
class Component:
    raw: str = ""            # exact text of the record (used when not dirty)
    name: str = ""
    symid: int = 0
    x: int = 0
    y: int = 0
    mirror: int = 0
    npins: int = 0
    m: int = 0
    nets: list[int] = field(default_factory=list)   # len npins+1
    macro: str = ""
    l4: list[int] = field(default_factory=list)     # [a, b, flags, rotation]
    label: str = ""
    label_bbox: list[int] | None = None
    designator: str = ""
    desig_bbox: list[int] | None = None
    s1: str = ""
    s1_bbox: list[int] | None = None
    extra_floats: list[str] | None = None
    pinattr: str = ""
    spice: str = ""
    s2: str = ""
    dtype: str = ""
    package: str = ""
    pinmap: list[int] = field(default_factory=list)  # len k+1
    l8: list[int] = field(default_factory=list)
    prefix: str = ""
    l3: list[int] = field(default_factory=list)
    tail: str = ""           # raw text of the trailing "c / values / d" part
    dirty: bool = False

    # l4[3] = orientation word: low 9 bits rotation (0/90/180/270), bit 9 (512)
    # mirror.  Older files leave garbage in the high bits, so always mask.
    @property
    def rotation(self) -> int:
        return (self.l4[3] & 511) if len(self.l4) > 3 else 0

    @property
    def mirrored(self) -> bool:
        return bool(self.l4[3] & 512) if len(self.l4) > 3 else False

    def serialize(self) -> str:
        if not self.dirty and self.raw:
            return self.raw
        out = fmt_str(self.name)
        out += fmt_ints([self.symid, self.x, self.y, self.mirror, self.npins, self.m])
        out += fmt_block10(self.nets)
        if self.extra_floats is not None:
            out += f"{len(self.extra_floats)}" + NL + "".join(
                " ".join(self.extra_floats[i:i + 10]) + NL
                for i in range(0, len(self.extra_floats), 10))
        out += fmt_str(self.macro)
        out += fmt_ints(self.l4)
        out += fmt_str(self.label)
        if self.label:
            out += fmt_ints(self.label_bbox or [0, 0, 0, 0])
        out += fmt_str(self.designator)
        if self.designator:
            out += fmt_ints(self.desig_bbox or [0, 0, 0, 0])
        out += fmt_str(self.s1)
        if self.s1:
            out += fmt_ints(self.s1_bbox or [0, 0, 0, 0])
        for s in (self.pinattr, self.spice, self.s2, self.dtype, self.package):
            out += fmt_str(s)
        out += f"{len(self.pinmap) - 1}\n"
        out += fmt_block10(self.pinmap, leading_newline=True)
        out += fmt_ints(self.l8)
        out += fmt_str(self.prefix)
        out += fmt_ints(self.l3)
        out += self.tail
        return out

    def summary(self) -> dict:
        return {
            "name": self.name, "designator": self.designator, "label": self.label,
            "symid": self.symid, "x": self.x, "y": self.y, "rotation": self.rotation,
            "mirror": self.mirror, "npins": self.npins,
        }


def _looks_like_component_start(r: Reader) -> bool:
    try:
        rr = Reader(r.s, r.i)
        name = rr.string()
        return bool(name) and len(rr.ints_line()) == 6
    except (CktError, ValueError):
        return False


def _is_extra_line(r: Reader) -> bool:
    """True if the next line is trailing device data rather than a new record."""
    ln = r.peek_line()
    if re.fullmatch(r"\d+", ln):          # wire count (after the last component)
        return False
    if _looks_like_component_start(r):
        return False
    return bool(re.fullmatch(r"[A-P]+", ln))


def parse_component(r: Reader) -> Component:
    start = r.i
    c = Component()
    c.name = r.string()
    l2 = r.ints_line()
    if len(l2) != 6:
        raise CktError(f"component line2 not 6 ints: {l2} ({c.name!r})")
    c.symid, c.x, c.y, c.mirror, c.npins, c.m = l2
    c.nets = r.int_block(c.npins + 1)
    # Signal generators carry a PWL table here: "<n>" then n floats.
    if re.fullmatch(r"[1-9]\d*", r.peek_line()):   # "0" is the empty macro string
        n = r.int_line()
        vals: list[str] = []
        while len(vals) < n:
            vals.extend(r.line().split())
        c.extra_floats = vals
    c.macro = r.string()
    c.l4 = r.ints_line(4)
    c.label = r.string()
    if c.label:
        c.label_bbox = r.ints_line(4)
    c.designator = r.string()
    if c.designator:
        c.desig_bbox = r.ints_line(4)
    c.s1 = r.string()
    if c.s1:
        c.s1_bbox = r.ints_line(4)
    c.pinattr = r.string()
    c.spice = r.string()
    c.s2 = r.string()
    c.dtype = r.string()
    c.package = r.string()
    k = r.int_line()
    blank = r.line()
    if blank.strip():
        raise CktError(f"expected blank line after pin-map count in {c.name!r}")
    c.pinmap = r.int_block(k + 1)
    c.l8 = r.ints_line()
    c.prefix = r.string()
    c.l3 = r.ints_line()
    tail_start = r.i
    cnt = r.int_line()
    if cnt:
        r.line()           # e.g. "46338.8 3"  (timestamp, component index)
    d = r.int_line()
    for _ in range(d):     # never observed non-zero; keep raw if it happens
        r.line()
    # Some devices append raw data lines (Data Sequencer memory as hex letters).
    while not r.eof() and _is_extra_line(r):
        r.line()
    c.tail = r.s[tail_start:r.i]
    c.raw = r.s[start:r.i]
    return c


# ----------------------------------------------------------------------- wires
@dataclass
class Wire:
    hdr: list[int]                 # 12 ints
    pts: list[tuple[int, int]]
    raw: str = ""
    dirty: bool = False

    # 1-based indices, 0 = none
    @property
    def pinA(self): return self.hdr[0]
    @property
    def pinB(self): return self.hdr[1]
    @property
    def net(self): return self.hdr[2]
    @property
    def devA(self): return self.hdr[7]
    @property
    def devB(self): return self.hdr[8]
    @property
    def wireA(self): return self.hdr[9]
    @property
    def wireB(self): return self.hdr[10]

    def serialize(self) -> str:
        if not self.dirty and self.raw:
            return self.raw
        h = list(self.hdr)
        h[11] = len(self.pts)
        return fmt_ints(h) + "".join(f"{x} {y}\n" for x, y in self.pts)


def parse_wire(r: Reader) -> Wire:
    start = r.i
    hdr = r.ints_line(12)
    pts = [tuple(r.ints_line(2)) for _ in range(hdr[11])]
    return Wire(hdr=hdr, pts=pts, raw=r.s[start:r.i])


# ----------------------------------------------------------------------- texts
@dataclass
class Note:
    raw: str
    text: str
    rect: list[int]


def parse_note(r: Reader) -> Note:
    start = r.i
    r.ints_line(13)          # LOGFONT
    r.string()               # face name
    r.ints_line(4)
    rect = r.ints_line(4)
    r.ints_line(4)
    text = r.string()
    return Note(raw=r.s[start:r.i], text=text, rect=rect)


# ------------------------------------------------------------------ whole file
@dataclass
class Circuit:
    header: str
    components: list[Component]
    wires: list[Wire]
    notes: list[Note]
    footer: str
    version: str = ""
    source: str = ""

    # ---------------------------------------------------------------- io
    @classmethod
    def load(cls, path: str | Path) -> "Circuit":
        return cls.parse(decode_bytes(Path(path).read_bytes()), source=str(path))

    @classmethod
    def parse(cls, text: str, source: str = "") -> "Circuit":
        lines = text.split("\n")
        if not lines or lines[0] != "CircuitMaker Text":
            raise CktError("not a 'CircuitMaker Text' file")
        version = lines[1].strip()
        # locate the component-count line: first line index whose remainder parses
        offsets = [0]
        for ln in lines[:-1]:
            offsets.append(offsets[-1] + len(ln) + 1)
        # The header has version-dependent length and ends with five title-block
        # strings, so we try every plausible "count" line and keep the parse that
        # yields the most objects (a stray "0" line would otherwise parse as an
        # empty circuit).
        start_line = 9
        if version == "5.6" and lines[2].startswith("Probes:"):
            start_line = 3 + 3 * int(lines[2].split(":")[1]) + 6
        best = None
        last_err = None
        for j in range(start_line, min(len(lines), start_line + 400)):
            if not re.fullmatch(r"\d+", lines[j]):
                continue
            try:
                r = Reader(text, offsets[j])
                n = r.int_line()
                comps = [parse_component(r) for _ in range(n)]
                nw = r.int_line()
                wires = [parse_wire(r) for _ in range(nw)]
                nt = r.int_line()
                notes = [parse_note(r) for _ in range(nt)]
            except (CktError, ValueError, IndexError) as e:
                last_err = e
                continue
            score = len(comps) + len(wires) + len(notes)
            if best is None or score > best[0]:
                best = (score, cls(header=text[:offsets[j]], components=comps, wires=wires,
                                   notes=notes, footer=text[r.i:], version=version, source=source))
            if comps:          # first non-empty parse is the real one
                break
        if best is None:
            raise CktError(f"could not locate component section ({last_err})")
        return best[1]

    def serialize(self) -> str:
        out = self.header
        out += f"{len(self.components)}\n"
        out += "".join(c.serialize() for c in self.components)
        out += f"{len(self.wires)}\n"
        out += "".join(w.serialize() for w in self.wires)
        out += f"{len(self.notes)}\n"
        out += "".join(n.raw for n in self.notes)
        out += self.footer
        return out

    def to_bytes(self) -> bytes:
        return encode_text(self.serialize())

    def save(self, path: str | Path) -> None:
        Path(path).write_bytes(self.to_bytes())

    # ------------------------------------------------------------ queries
    def comp(self, designator: str) -> Component:
        for c in self.components:
            if c.designator == designator:
                return c
        raise KeyError(designator)

    def ref(self, idx1: int) -> str:
        """Human name for a 1-based component index."""
        c = self.components[idx1 - 1]
        return c.designator or f"#{idx1}:{c.name}"


# --------------------------------------------------------------------- netlist
def build_nets(ckt: Circuit, with_wires: bool = False):
    """Union-find over wire metadata.

    Returns a list of nets; each net is a set of (component_index_1based, pin_1based).
    Wires whose end lands on another wire are merged with that wire.
    Net labels / bus wires are not handled here (see ``named_nets``).
    """
    parent: dict = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for wi, w in enumerate(ckt.wires, start=1):
        node = ("w", wi)
        find(node)
        for dev, pin, wref in ((w.devA, w.pinA, w.wireA), (w.devB, w.pinB, w.wireB)):
            if dev:
                union(node, ("p", dev, pin))
            elif wref:
                union(node, ("w", wref))
    # Global nets: a component whose pin attribute is a bare "NAME;" (Ground~ ->
    # "GND;", +V -> "VCC;") ties its pin to the global net NAME, exactly like
    # CircuitMaker does (verified: two Ground symbols end up in one net).
    for i, c in enumerate(ckt.components, start=1):
        m = re.fullmatch(r"\s*([A-Za-z_][\w+\-]*);\s*", c.pinattr or "")
        if m:
            union(("p", i, 1), ("g", m.group(1).upper()))
    groups: dict = {}
    for k in list(parent):
        if k[0] == "p":
            groups.setdefault(find(k), set()).add((k[1], k[2]))
    if with_wires:
        roots = list(groups)
        wire_net = {}
        for wi in range(1, len(ckt.wires) + 1):
            r = find(("w", wi))
            wire_net[wi] = roots.index(r) if r in groups else None
        return [groups[r] for r in roots], wire_net
    return [g for g in groups.values()]


def describe(ckt: Circuit, pin_names=None) -> str:
    """Readable dump.  pin_names: callable(component)->list[str] or None."""
    out = [f"version {ckt.version}  components={len(ckt.components)} wires={len(ckt.wires)} notes={len(ckt.notes)}"]
    for i, c in enumerate(ckt.components, start=1):
        out.append(f"  [{i}] {c.designator or '-':6} {c.name:18} label={c.label!r:10} sym={c.symid} at=({c.x},{c.y}) rot={c.rotation} mirror={c.mirror} pins={c.npins}")
    nets = build_nets(ckt)
    out.append("nets (from wire metadata):")
    for k, n in enumerate(sorted(nets, key=lambda s: sorted(s)), start=1):
        items = []
        for dev, pin in sorted(n):
            c = ckt.components[dev - 1]
            pname = None
            if pin_names:
                names = pin_names(c)
                if names and 0 < pin <= len(names):
                    pname = names[pin - 1]
            items.append(f"{ckt.ref(dev)}.{pname or pin}")
        out.append(f"  N{k}: " + ", ".join(items))
    for n in ckt.notes:
        out.append(f"  note: {n.text!r}")
    return "\n".join(out)


# ------------------------------------------------------------------------- cli
def _cmd_roundtrip(paths):
    bad = 0
    for p in paths:
        raw = Path(p).read_bytes()
        try:
            ck = Circuit.parse(decode_bytes(raw))
            ok = ck.to_bytes() == raw
        except CktError as e:
            ok = False
            print(f"FAIL  {p}: {e}")
            bad += 1
            continue
        print(f"{'OK  ' if ok else 'DIFF'}  {p}  v{ck.version} comps={len(ck.components)} wires={len(ck.wires)}")
        bad += 0 if ok else 1
    return 1 if bad else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("roundtrip", help="verify parse->write is byte identical")
    s.add_argument("files", nargs="+")
    s = sub.add_parser("inspect", help="list components and wire-metadata nets")
    s.add_argument("file")
    s.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "roundtrip":
        return _cmd_roundtrip(a.files)
    if a.cmd == "inspect":
        ck = Circuit.load(a.file)
        try:
            from devlib import pin_names_for  # optional, adds pin names
        except Exception:  # pragma: no cover
            pin_names_for = None
        if a.json:
            print(json.dumps({"components": [c.summary() for c in ck.components],
                              "nets": [sorted(n) for n in build_nets(ck)]}, indent=1))
        else:
            print(describe(ck, pin_names_for))
    return 0


if __name__ == "__main__":
    sys.exit(main())
