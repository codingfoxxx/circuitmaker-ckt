#!/usr/bin/env python3
"""Generate CircuitMaker 2000 ``.CKT`` schematics from Python.

    from build import Schematic
    s = Schematic()
    s.add("Pulser", "CLK", 80, 200, high=5, low=5)
    s.add("74LS90", "U1", 260, 200)
    s.add("Ground", "GND1", 180, 320)
    s.wire("CLK.Q1", "U1.CP0")                      # 2-pin net, auto L/Z route
    s.wire("U1.Q0", "U1.CP1", via=[(300, 240), (210, 240)])   # explicit route
    s.net("GND1.1", "U1.MS1", "U1.MS2", trunk_x=200)            # multi-pin net
    s.text("Divisor por 12", 80, 40)
    s.save("out.CKT")           # then: python validate.py out.CKT

How it works
* Every component record is cloned from a record that CircuitMaker itself
  wrote (``assets/templates.json``, harvested from app-saved files), then
  position / designator / model / pin maps are patched.  ICs not in the
  template set are synthesised from a template of the same symbol plus the
  data in Device.lib, Devicedb.dat, SEARCHDB.DAT and Models/*.MOD.
* Connectivity is written in each wire's metadata (device/pin or wire it
  lands on) because that is what CircuitMaker uses; geometry is only drawn.
  The router therefore only has to make the drawing readable: every net gets
  its own vertical track, wires start/end exactly on pin tips, multi-pin
  nets are a trunk plus T-junction branches.
* Components are always placed unrotated (rotation pivots differ per symbol).
* Net ids are left 0: CircuitMaker recomputes them on load/save.
"""
from __future__ import annotations

import datetime as _dt
import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import ckt
import devlib

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# friendly names -> symbol names used in .CKT
ALIASES = {
    "pulser": "Pulser~", "clock": "Pulser~",
    "switch": "Logic Switch~", "logic switch": "Logic Switch~",
    "led": "Logic Display~", "logic display": "Logic Display~", "probe led": "Logic Display~",
    "hex": "Hex Display~", "hex display": "Hex Display~",
    "scope": "SCOPE", "probe": "SCOPE",
    "ground": "Ground~", "gnd": "Ground~",
    "+v": "+V", "vcc": "+V",
    "signal gen": "Signal Gen~",
    "data seq": "Data Seq~",
}


def _templates() -> dict[str, str]:
    return json.loads((ASSETS / "templates.json").read_text("utf-8"))["records"]


def _blank() -> dict:
    return json.loads((ASSETS / "blank56.json").read_text("utf-8"))


def _tdatetime(now=None) -> str:
    now = now or _dt.datetime.now()
    days = (now - _dt.datetime(1899, 12, 30)).total_seconds() / 86400.0
    return f"{days:.6g}"


def _parse_ep(ep: str) -> tuple[str, str]:
    if "." not in ep:
        raise ValueError(f"endpoint must be 'REF.PIN', got {ep!r}")
    ref, pin = ep.split(".", 1)
    return ref, pin


@dataclass
class Placed:
    ref: str
    comp: ckt.Component
    symbol: devlib.Symbol
    index: int                     # 1-based position in the file
    model: str = ""

    def pin(self, key: str | int) -> devlib.Pin:
        if isinstance(key, str) and key.isdigit():
            key = int(key)
        return self.symbol.pin(key)

    def tip(self, key) -> tuple[int, int]:
        tx, ty = self.pin(key).tip()
        return self.comp.x + tx, self.comp.y + ty

    def box(self) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = self.symbol.body_box()
        return self.comp.x + x0, self.comp.y + y0, self.comp.x + x1, self.comp.y + y1


@dataclass
class _Wire:
    a: tuple            # ("p", ref, pin) or ("w", wire_index)
    b: tuple
    pts: list
    net: str


class Schematic:
    def __init__(self):
        b = _blank()
        self.header = b["header"]
        self.footer = b["footer"]
        self.note_example = b["note_example"]
        self.parts: dict[str, Placed] = {}
        self.order: list[Placed] = []
        self.wires: list[_Wire] = []
        self.notes: list[ckt.Note] = []
        self._ids = set()
        self._stamp = _tdatetime()
        self._netcount = 0

    # ------------------------------------------------------------ editing
    @classmethod
    def load(cls, path: str | Path) -> "Schematic":
        """Open an existing .CKT for editing (add/remove parts and wires, then save).

        Untouched component records are written back byte-identical; wires are
        rewritten from their metadata (CircuitMaker recomputes net ids/flags)."""
        c = ckt.Circuit.load(path)
        s = cls()
        s.header, s.footer = c.header, c.footer
        s.notes = list(c.notes)
        refs = {}
        for i, comp in enumerate(c.components, 1):
            ref = comp.designator or f"#{i}"
            while ref in s.parts:
                ref += "'"
            try:
                sym = devlib.symbol(comp.name)
            except KeyError:
                sym = devlib.Symbol(comp.name, comp.symid, 0, [])
            p = Placed(ref, comp, sym, i, comp.label)
            s.parts[ref] = p
            s.order.append(p)
            refs[i] = ref
            if comp.l3:
                s._ids.add(comp.l3[0])
        for w in c.wires:
            ends = []
            for dev, pin, wref in ((w.devA, w.pinA, w.wireA), (w.devB, w.pinB, w.wireB)):
                ends.append(("p", refs[dev], pin) if dev else ("w", wref) if wref else ("n",))
            s.wires.append(_Wire(ends[0], ends[1], list(w.pts), ""))
        return s

    def remove_wire(self, idx: int):
        """Delete wire ``idx`` (1-based); references to later wires are renumbered,
        wires that landed on it become dangling at that end (validate.py warns)."""
        del self.wires[idx - 1]
        for w in self.wires:
            for attr in ("a", "b"):
                e = getattr(w, attr)
                if e[0] == "w":
                    if e[1] == idx:
                        setattr(w, attr, ("n",))
                    elif e[1] > idx:
                        setattr(w, attr, ("w", e[1] - 1))

    def remove(self, ref: str, keep_wires: bool = False):
        """Delete a component and (unless keep_wires) every wire attached to it."""
        p = self.parts.pop(ref)
        self.order.remove(p)
        for k, q in enumerate(self.order, 1):
            q.index = k
        if not keep_wires:
            for i in range(len(self.wires), 0, -1):
                w = self.wires[i - 1]
                if any(e[0] == "p" and e[1] == ref for e in (w.a, w.b)):
                    self.remove_wire(i)
        else:
            for w in self.wires:
                for attr in ("a", "b"):
                    e = getattr(w, attr)
                    if e[0] == "p" and e[1] == ref:
                        setattr(w, attr, ("n",))

    def wire_at(self, x: int, y: int) -> list[int]:
        """Wire ids whose drawing passes through (x, y)."""
        return [i for i, w in enumerate(self.wires, 1) if _on(w.pts, (x, y))]

    def wires_of(self, ep: str) -> list[int]:
        """Wire ids attached to pin 'REF.PIN'."""
        ref, pin = _parse_ep(ep)
        idx = self.parts[ref].pin(pin).index
        return [i for i, w in enumerate(self.wires, 1)
                if any(e[0] == "p" and e[1] == ref and e[2] == idx for e in (w.a, w.b))]

    # ------------------------------------------------------------ components
    def add(self, model: str, ref: str, x: int, y: int, part: str | None = None, **params) -> Placed:
        """Place a device.

        model: '74LS90', '4011', '74LS08', 'Pulser', 'Logic Switch', 'Hex Display',
               'Logic Display', 'SCOPE', 'Ground', '+V', or any Device.lib symbol name.
        ref:   designator; for multi-gate ICs give e.g. 'U3A' (or ref='U3', part='A').
        params: Pulser(high=, low=), Logic Switch(on=), SCOPE(label=).
        """
        if ref in self.parts:
            raise ValueError(f"duplicate designator {ref}")
        templates = _templates()
        sym_name = ALIASES.get(model.lower())
        resolved = None
        if sym_name is None:
            if model in templates or model + "~" in templates:
                sym_name = model if model in templates else model + "~"
            if sym_name is None or sym_name in ("74LS90",) or re.match(r"^(74|40|45)", model):
                resolved = devlib.resolve_model(model)
                sym_name = resolved["symbol"].name
        sym = devlib.symbol(sym_name)
        raw = templates.get(sym_name) or self._template_for_symbol(sym_name, templates)
        comp = ckt.Circuit.parse(self.header + "1\n" + raw + "0\n0\n" + self.footer).components[0]
        comp.dirty = True
        npin = len(sym.pins)
        comp.x, comp.y = int(x), int(y)
        comp.mirror = 0
        comp.l4 = [0, 0, comp.l4[2], 0]
        keep_tail = comp.nets[npin + 1:]
        comp.nets = [comp.nets[0]] + [0] * npin + keep_tail      # [0] is not a net (Hex Display keeps 10)
        comp.npins = len(comp.nets) - 1
        # designator / part
        m = re.fullmatch(r"(.*\d)([A-H])", ref)
        if part is None and m and resolved and len(resolved["mod"]["parts"]) > 1:
            base, part = m.group(1), m.group(2)
        comp.designator = ref
        if resolved:
            self._apply_model(comp, sym, resolved, part)
        self._apply_params(comp, sym_name, params)
        # display flags in l4[2]: 0x200 shows the designator, 0x1000 the label/model
        show = params.get("show")
        if show is None and resolved:
            show = ("ref", "label") if len(resolved["mod"]["parts"]) <= 1 else ("ref",)
        if show is not None:
            f = comp.l4[2] & ~(0x200 | 0x1000)
            if "ref" in show:
                f |= 0x200
            if "label" in show:
                f |= 0x1000
            comp.l4[2] = f
        # designator text box: templates synthesized from another IC (or old v5
        # records with a 0-box) put "U1" on top of the model name; put it 10 px
        # above the label box, as CM does for its own records (74LS93: -35/-45).
        lb, db = comp.label_bbox, comp.desig_bbox
        if comp.designator and lb and lb != [0, 0, 0, 0]:
            if not db or db == [0, 0, 0, 0] or not (db[3] <= lb[1] or db[1] >= lb[3]):
                w = 4 * len(comp.designator)
                comp.desig_bbox = [-w, lb[1] - 10, w, lb[3] - 10]
        uid = random.randint(100, 9999)
        while uid in self._ids:
            uid = random.randint(100, 9999)
        self._ids.add(uid)
        if comp.l3:
            comp.l3[0] = uid
        p = Placed(ref, comp, sym, len(self.order) + 1, resolved["model"] if resolved else sym_name)
        self.parts[ref] = p
        self.order.append(p)
        return p

    def _template_for_symbol(self, sym_name, templates):
        """Synthesize a record for a symbol that no app-saved file contains, by
        cloning a structurally similar digital record (a gate for gates, an IC
        otherwise) and patching name / symbol id / pin count / SPICE line from
        Device.lib.  Always confirm with CircuitMaker (open + resave + compare
        nets: scripts/cm_verify.py) before trusting a synthesized part."""
        sym = devlib.symbol(sym_name)
        gate_like = bool(re.match(r"\d-In ", sym_name)) or sym_name in ("Inverter~", "Buffer~")
        donor_name = "2-In AND~" if gate_like else "74LS90"
        donor = templates.get(donor_name)
        if not donor:
            raise KeyError(f"no template for {sym_name!r} and no donor {donor_name!r}")
        c = ckt.Circuit.parse(self.header + "1\n" + donor + "0\n0\n" + self.footer).components[0]
        c.dirty = True
        c.name = sym_name
        c.symid = sym.symid
        c.nets = [0] * (len(sym.pins) + 1)
        c.npins = len(sym.pins)
        dev = next((d for d in (sym.devices or []) if d.get("spice")), None)
        if dev:
            c.spice = dev["spice"]
            c.pinattr = dev["pinattr"] or c.pinattr
            c.label = dev["model"] or c.label
            c.dtype = dev["type"] or c.dtype
            c.package = dev["package"] or c.package
        return c.serialize()

    def _apply_model(self, comp, sym, resolved, part):
        mod = resolved["mod"]
        model = resolved["model"]
        comp.label = model
        dev = next((d for d in (sym.devices or []) if d.get("spice")), None)
        if mod["pinattr"]:
            comp.pinattr = mod["pinattr"]
        elif dev and dev["pinattr"]:
            comp.pinattr = dev["pinattr"]
        if dev and dev["spice"]:
            comp.spice = dev["spice"]
        comp.dtype = mod["type"] or (dev["type"] if dev else comp.dtype) or comp.dtype
        comp.package = mod["package"] or (dev["package"] if dev else comp.package) or comp.package
        parts = mod["parts"]
        if parts:
            idx = (ord(part) - ord("A")) if part else 0
            if idx >= len(parts):
                raise ValueError(f"{model} has only {len(parts)} parts")
            allpins = [p for grp in parts for p in grp]
            comp.pinmap = [0] + parts[idx] + allpins + [0]
            if len(comp.l8) > 5:
                comp.l8[4] = len(parts)
                comp.l8[5] = idx + 1 if len(parts) > 1 else 0

    def _apply_params(self, comp, sym_name, params):
        if sym_name == "Pulser~":
            high = int(params.get("high", 5))
            low = int(params.get("low", 5))
            # nets layout: [0, CP1, CP2, Q1, Q2, ?, ?, LOW, HIGH, ?]
            # (verified: wrote 7/3 here, CM's Edit Pulser dialog showed Low=7 High=3)
            comp.nets[7], comp.nets[8] = low, high
        elif sym_name == "Logic Switch~":
            # State verified in CM (same switch saved OFF and ON): ON is stored as an
            # 11-entry nets array with nets[10] = 1; OFF is just [0, net].
            # The label ("5V"/"0V") is only text -- CM ignores it at Reset.
            on = bool(params.get("on", False))
            comp.label = "5V" if on else "0V"
            comp.nets = comp.nets[:2] + ([0] * 8 + [1] if on else [])
            comp.npins = len(comp.nets) - 1
        elif sym_name == "SCOPE":
            if "label" in params:
                comp.label = str(params["label"])
        elif sym_name == "+V":
            # +V is a DC supply whose value is its label; the only app-saved
            # template is 741.ckt's "Vee" at -12V -- never keep that for logic.
            comp.label = str(params.get("volts", "+5V"))
            comp.label_bbox = [-12, -14, 12, -6]
        elif sym_name == "Hex Key~":
            # nets tail: [..., value, ord(key char)]  (Alarm2/Lock/Rocket: "2 50", "3 51")
            v = int(params.get("value", 0)) & 15
            while len(comp.nets) < 12:
                comp.nets.append(0)
            comp.nets[10], comp.nets[11] = v, ord("0123456789ABCDEF"[v])
            comp.npins = len(comp.nets) - 1
            if not comp.desig_bbox or comp.desig_bbox == [0, 0, 0, 0]:
                comp.desig_bbox = [-30, -24, -12, -16]
        if "label" in params and sym_name not in ("SCOPE",):
            comp.label = str(params["label"])

    # ----------------------------------------------------------------- wires
    def _ep(self, ep):
        ref, pin = _parse_ep(ep)
        if ref not in self.parts:
            raise KeyError(f"unknown component {ref}")
        part = self.parts[ref]
        p = part.pin(pin)
        return ref, p.index, part.tip(p.index), p.side

    def _new_net(self, name):
        self._netcount += 1
        return name or f"N{self._netcount}"

    STUB = 10

    def _with_stubs(self, pts, side_a, side_b=None, stub=None):
        """Make the first (and last) segment leave the pin outward along its axis,
        inserting a stub point if the route would otherwise turn on the pin tip
        or run back over the component."""
        stub = self.STUB if stub is None else stub
        vec = devlib.SIDE_VEC

        def fix(seq, side):
            if side not in vec or len(seq) < 2:
                return seq
            (x0, y0), (x1, y1) = seq[0], seq[1]
            dx, dy = vec[side]
            along = (x1 - x0) * dx + (y1 - y0) * dy
            straight = (dx and y1 == y0) or (dy and x1 == x0)
            if straight and along > 0:
                return seq
            return [seq[0], (x0 + dx * stub, y0 + dy * stub)] + seq[1:]
        pts = fix(list(pts), side_a)
        if side_b:
            pts = list(reversed(fix(list(reversed(pts)), side_b)))
        return pts

    def wire(self, a: str, b: str, via: list | None = None, x: int | None = None,
             name: str | None = None, stub: int | None = None):
        """Connect two pins with one wire.  Route: explicit ``via`` points, or an
        L/Z shape using vertical track ``x`` (default: midpoint).  The wire always
        leaves each pin outward first (stub)."""
        ra, ia, (xa, ya), sa = self._ep(a)
        rb, ib, (xb, yb), sb = self._ep(b)
        if via:
            pts = [(xa, ya)] + [tuple(v) for v in via] + [(xb, yb)]
        elif ya == yb:
            pts = [(xa, ya), (xb, yb)]
        else:
            tx = x if x is not None else (xa + xb) // 2
            pts = [(xa, ya), (tx, ya), (tx, yb), (xb, yb)]
        pts = _orthogonalize(self._with_stubs(_orthogonalize(pts), sa, sb, stub))
        self.wires.append(_Wire(("p", ra, ia), ("p", rb, ib), pts, self._new_net(name)))
        return len(self.wires)

    def net(self, *eps: str, trunk_x: int | None = None, trunk_y: int | None = None,
            name: str | None = None):
        """Connect several pins: vertical trunk at ``trunk_x`` (or horizontal at
        ``trunk_y``) with T-junction branches.  First wire spans the trunk."""
        if len(eps) < 2:
            raise ValueError("net needs >= 2 endpoints")
        if len(eps) == 2 and trunk_x is None and trunk_y is None:
            return self.wire(eps[0], eps[1], name=name)
        info = [self._ep(e) for e in eps]
        netname = self._new_net(name)
        if trunk_y is not None:
            info.sort(key=lambda t: t[2][0])
            (r0, i0, (x0, y0), _), (r1, i1, (x1, y1), _) = info[0], info[-1]
            first = _orthogonalize([(x0, y0), (x0, trunk_y), (x1, trunk_y), (x1, y1)])
            self.wires.append(_Wire(("p", r0, i0), ("p", r1, i1), first, netname))
            trunk = len(self.wires)
            for r, i, (x, y), s in info[1:-1]:
                self.wires.append(_Wire(("p", r, i), ("w", trunk), _orthogonalize([(x, y), (x, trunk_y)]), netname))
            return trunk
        tx = trunk_x if trunk_x is not None else sum(t[2][0] for t in info) // len(info)
        info.sort(key=lambda t: t[2][1])
        (r0, i0, (x0, y0), s0), (r1, i1, (x1, y1), s1) = info[0], info[-1]
        first = _orthogonalize(self._with_stubs(_orthogonalize([(x0, y0), (tx, y0), (tx, y1), (x1, y1)]), s0, s1))
        self.wires.append(_Wire(("p", r0, i0), ("p", r1, i1), first, netname))
        trunk = len(self.wires)
        for r, i, (x, y), s in info[1:-1]:
            pts = _orthogonalize(self._with_stubs([(x, y), (tx, y)], s))
            self.wires.append(_Wire(("p", r, i), ("w", trunk), pts, netname))
        return trunk

    def branch(self, ep: str, to_wire: int, point: tuple[int, int] | None = None,
               via: list | None = None):
        """Extra pin joining an existing wire (T-junction).  The end point must lie
        on that wire; default is the projection of the pin tip onto the wire."""
        r, i, (x, y), s = self._ep(ep)
        target = self.wires[to_wire - 1]
        if point is None:
            point = _project(target.pts, (x, y))
        pts = [(x, y)] + [tuple(v) for v in (via or [])] + [tuple(point)]
        pts = _orthogonalize(self._with_stubs(_orthogonalize(pts), s))
        if not _on(target.pts, pts[-1]):
            raise ValueError(f"branch end {pts[-1]} is not on wire {to_wire} {target.pts}")
        self.wires.append(_Wire(("p", r, i), ("w", to_wire), pts, target.net))
        return len(self.wires)

    def segment(self, from_wire: int, to_wire: int, pts: list):
        """A pure wire-to-wire jumper (both ends T-joined to existing wires)."""
        pts = _orthogonalize([tuple(p) for p in pts])
        for w, p in ((from_wire, pts[0]), (to_wire, pts[-1])):
            if not _on(self.wires[w - 1].pts, p):
                raise ValueError(f"{p} is not on wire {w}")
        self.wires.append(_Wire(("w", from_wire), ("w", to_wire), pts, self.wires[from_wire - 1].net))
        return len(self.wires)

    # ----------------------------------------------------------------- notes
    def text(self, text: str, x: int, y: int, size: int = 13, font: str = "Times New Roman"):
        lines = text.split("\n")
        w = max(len(l) for l in lines) * max(6, size * 6 // 10) + 8
        h = len(lines) * (size + 3) + 8
        body = text.replace("\n", "\r\n")
        raw = (f"-{size} 0 0 0 400 0 0 0 0 3 2 1 18\n" + ckt.fmt_str(font) +
               f"0 0 0 {len(body)}\n{x} {y} {x + w} {y + h}\n{x + 4} {y + 4} {x + w + 1} {y + h - 2}\n" +
               ckt.fmt_str(body))
        self.notes.append(ckt.Note(raw=raw, text=body, rect=[x, y, x + w, y + h]))

    # ---------------------------------------------------------------- output
    def to_circuit(self) -> ckt.Circuit:
        comps = []
        for k, p in enumerate(self.order):
            c = p.comp
            p.index = k + 1
            if c.dirty:
                c.tail = f"2\n{self._stamp} {k}\n0\n"
            comps.append(c)
        wires = []
        for w in self.wires:
            hdr = [0] * 12
            for slot, end in ((0, w.a), (1, w.b)):
                if end[0] == "p":
                    hdr[slot] = end[2]
                    hdr[7 + slot] = self.parts[end[1]].index
                elif end[0] == "w":
                    hdr[9 + slot] = end[1]
            hdr[11] = len(w.pts)
            wires.append(ckt.Wire(hdr=hdr, pts=[tuple(map(int, p)) for p in w.pts], dirty=True))
        return ckt.Circuit(header=self.header, components=comps, wires=wires, notes=self.notes,
                           footer=self.footer, version="5.6")

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        circ = self.to_circuit()
        circ.save(path)
        # sanity: must parse back identically
        back = ckt.Circuit.load(path)
        assert len(back.components) == len(self.order) and len(back.wires) == len(self.wires)
        return path


def _orthogonalize(pts):
    """Insert corners so every segment is horizontal or vertical; drop dups."""
    out = [tuple(pts[0])]
    for p in pts[1:]:
        p = tuple(p)
        q = out[-1]
        if p == q:
            continue
        if p[0] != q[0] and p[1] != q[1]:
            out.append((p[0], q[1]))
        out.append(p)
    # remove collinear middle points
    res = [out[0]]
    for i in range(1, len(out) - 1):
        a, b, c = res[-1], out[i], out[i + 1]
        if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
            continue
        res.append(b)
    if len(out) > 1:
        res.append(out[-1])
    return res


def _on(pts, p):
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 == x1 == p[0] and min(y0, y1) <= p[1] <= max(y0, y1):
            return True
        if y0 == y1 == p[1] and min(x0, x1) <= p[0] <= max(x0, x1):
            return True
    return False


def _project(pts, p):
    """Closest point on an orthogonal polyline to p (integer coords)."""
    best, bd = None, None
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 == x1:
            q = (x0, min(max(p[1], min(y0, y1)), max(y0, y1)))
        else:
            q = (min(max(p[0], min(x0, x1)), max(x0, x1)), y0)
        d = abs(q[0] - p[0]) + abs(q[1] - p[1])
        if bd is None or d < bd:
            best, bd = q, d
    return best
