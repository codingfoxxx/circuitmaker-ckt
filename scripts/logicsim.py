#!/usr/bin/env python3
"""Event-driven digital simulator for CircuitMaker 2000 .CKT files.

Purpose: prove the *logic* of a schematic independently of the GUI, using the
exact connectivity CircuitMaker uses (wire metadata).  Values are 0/1/X.

    python logicsim.py COUNTER.CKT --clock CLK --pulses 100 --bcd U2,U1
    python logicsim.py DIVIDER.CKT --clock CLK --pulses 120 --freq U2.Q0
    python logicsim.py STOPN.CKT --clock CLK --pulses 60 --bcd U2,U1 \
         --script "50:RST=1,52:RST=0"          # toggle switch RST after pulse 50 / 52
    add --sweep 50 to repeat with random propagation delays (race detection)

Python API: ``Sim.load(path)``, ``sim.set(ref, v)``, ``sim.run(ticks)``,
``sim.get('U1.Q0')``, ``sim.bcd('U1')``, ``sim.pulse(clock_ref)``.

Implemented models (add more in make_device): gates of any width (AND/NAND/
OR/NOR/XOR/XNOR/NOT/BUF, 74xx symbols and 4001/4011/4069/4070/4071/4077/4081/
4093/4023/4025/4073/4075/4012/4002/4082/4072/4049/4050), 74LS90/92/93 (with
their internal ripple structure), 74LS74, 74LS73/76/107/112, 4013, 4027,
74LS192/193, 74LS160/161/162/163, 74LS47/48, 4511 (decoders: data inputs only),
74LS165 (PISO), 74LS164 (SIPO); sources Pulser, Logic Switch (state = nets[10]),
Hex Key (value = nets[10]), +V (value = label), Ground; sinks Logic/Hex Display,
SCOPE, 7-seg.  ``sim.hexdisp(ref)`` reads a Hex Display.
Any other digital part (e.g. 74LS138/151/157/83/85/190/191/194, 4017, 4518)
raises NotImplementedError instead of being guessed -- write its model first,
using the pin names printed by `python devlib.py show <symbol>`.

Conventions: unconnected TTL (74xx) inputs read 1, CMOS (40xx) read X.
Pulser: Q1 starts LOW for `low` ticks then HIGH for `high` ticks... is
configurable (``--pulser-start``); counting tests drive clocks with pulse()
so phase does not matter.
"""
from __future__ import annotations

import argparse
import heapq
import random
import re
import sys
from collections import defaultdict

import ckt
import devlib

X = "X"


def _b(v):
    return v if v in (0, 1) else X


def AND(vals):
    if any(v == 0 for v in vals):
        return 0
    return 1 if all(v == 1 for v in vals) else X


def OR(vals):
    if any(v == 1 for v in vals):
        return 1
    return 0 if all(v == 0 for v in vals) else X


def NOT(v):
    return {0: 1, 1: 0}.get(v, X)


def XOR(vals):
    if any(v == X for v in vals):
        return X
    return sum(vals) % 2


# ------------------------------------------------------------------ device base
class Device:
    delay = 1

    def __init__(self, ref, pins, sim):
        self.ref = ref
        self.pins = pins            # pin name -> net id (or None)
        self.sim = sim
        self.prev = {}

    def inp(self, name):
        return self.sim.read(self.pins.get(name), self)

    def rising(self, name):
        old = self.prev.get(name, X)
        return old == 0 and self.inp(name) == 1

    def falling(self, name):
        old = self.prev.get(name, X)
        return old == 1 and self.inp(name) == 0

    def drive(self, name, value, delay=None):
        net = self.pins.get(name)
        if net is not None:
            self.sim.schedule(net, self, value, self.sim.delay_for(self, delay, key=name))

    def snapshot(self):
        self.prev = {n: self.inp(n) for n in self.pins}

    def evaluate(self):  # called on any input change
        pass

    def init(self):
        pass


class Gate(Device):
    def __init__(self, ref, pins, sim, func, n_in):
        super().__init__(ref, pins, sim)
        self.func, self.n_in = func, n_in
        self.in_names = [k for k in pins if k != "O"]

    def evaluate(self):
        vals = [self.inp(n) for n in self.in_names]
        f = self.func
        if f == "AND": v = AND(vals)
        elif f == "NAND": v = NOT(AND(vals))
        elif f == "OR": v = OR(vals)
        elif f == "NOR": v = NOT(OR(vals))
        elif f == "XOR": v = XOR(vals)
        elif f == "XNOR": v = NOT(XOR(vals))
        elif f == "NOT": v = NOT(vals[0])
        else: v = vals[0]
        self.drive("O", v)

    init = evaluate


class Ripple90(Device):
    """74LS90 / 74LS92 / 74LS93 with their real internal flip-flop structure
    (TI datasheets), so transient states during ripple are realistic:

      A: toggles on CP0 falling                       -> Q0
      7490 /5 : B: J=/QD K=1 on CP1;  C: toggles on QB falling;  D: J=QB*QC K=1 on CP1
      7492 /6 : B: J=/QC K=1 on CP1;  C: J=QB K=1 on CP1;  D: toggles on QC falling
      7493 /8 : B on CP1, C on QB falling, D on QC falling (all toggle)
    MR = MR1*MR2 clears, MS = MS1*MS2 (7490 only) sets 9 and has priority.
    Each internal flip-flop has its own (fixed) delay, see Sim.delay_for."""

    def __init__(self, ref, pins, sim, kind):
        super().__init__(ref, pins, sim)
        self.kind = kind
        self.q = {"A": 0, "B": 0, "C": 0, "D": 0}
        self.gen = 0
        self.outpin = {"A": "Q0", "B": "Q1", "C": "Q2", "D": "Q3"}

    def _set(self, ff, v):
        """Schedule flip-flop ``ff`` to become v after its delay, then cascade."""
        gen = self.gen
        d = self.sim.delay_for(self, None, key=ff)

        def apply():
            if gen != self.gen:
                return                       # cancelled by an async reset/set
            old = self.q[ff]
            self.q[ff] = v
            self.drive(self.outpin[ff], v, delay=0)
            if old == 1 and v == 0:          # internal ripple clocks
                if self.kind in ("90", "93") and ff == "B":
                    self._set("C", NOT(self.q["C"]))
                if self.kind == "93" and ff == "C":
                    self._set("D", NOT(self.q["D"]))
                if self.kind == "92" and ff == "C":
                    self._set("D", NOT(self.q["D"]))
        self.sim.at(self.sim.now + d, apply)

    def _force(self, values):
        self.gen += 1
        for ff, v in values.items():
            if self.q[ff] != v or True:
                self.q[ff] = v
                self.drive(self.outpin[ff], v, delay=self.sim.delay_for(self, None, key=ff))

    def evaluate(self):
        mr = AND([self.inp("MR1"), self.inp("MR2")])
        ms = AND([self.inp("MS1"), self.inp("MS2")]) if "MS1" in self.pins else 0
        if ms == 1:                        # set to 9 has priority (7490)
            self._force({"A": 1, "B": 0, "C": 0, "D": 1})
            return
        if mr == 1:
            self._force({"A": 0, "B": 0, "C": 0, "D": 0})
            return
        q = self.q
        if self.falling("CP0"):
            self._set("A", NOT(q["A"]))
        if self.falling("CP1"):
            if self.kind == "90":
                b = 1 if (q["D"] == 0 and q["B"] == 0) else 0 if q["B"] == 1 else q["B"]   # J=/QD, K=1
                d = 1 if (q["B"] == 1 and q["C"] == 1) else 0                               # J=QB*QC, K=1
                self._set("B", b)
                if d != q["D"]:
                    self._set("D", d)
            elif self.kind == "92":
                b = 1 if (q["C"] == 0 and q["B"] == 0) else 0 if q["B"] == 1 else q["B"]   # J=/QC, K=1
                c = 1 if (q["B"] == 1 and q["C"] == 0) else 0 if q["C"] == 1 else q["C"]   # J=QB, K=1
                self._set("B", b)
                if c != q["C"]:
                    self._set("C", c)
            else:
                self._set("B", NOT(q["B"]))

    def init(self):
        for ff, pin in self.outpin.items():
            self.drive(pin, self.q[ff], delay=0)


class DFF(Device):
    """74LS74 (two FFs, active-low S/R, rising edge) / 4013 (active-high S/R)."""

    def __init__(self, ref, pins, sim, units, active_low=True):
        super().__init__(ref, pins, sim)
        self.units = units          # list of dict(cp, d, s, r, q, qn)
        self.q = [0] * len(units)
        self.al = active_low

    def evaluate(self):
        for i, u in enumerate(self.units):
            s, r = self.inp(u["s"]), self.inp(u["r"])
            if self.al:
                s, r = NOT(s), NOT(r)
            if s == 1 or r == 1:
                self.q[i] = 1 if s == 1 else 0
                self.drive(u["q"], 1 if s == 1 else 0)
                self.drive(u["qn"], 1 if (r == 1 and s != 1) or (s == 1 and r == 1) else 0)
                continue
            if self.rising(u["cp"]):
                self.q[i] = self.inp(u["d"])
            self.drive(u["q"], self.q[i])
            self.drive(u["qn"], NOT(self.q[i]))

    def init(self):
        for i, u in enumerate(self.units):
            self.drive(u["q"], self.q[i]); self.drive(u["qn"], NOT(self.q[i]))


class JKFF(Device):
    def __init__(self, ref, pins, sim, units, falling=True, active_low=True):
        super().__init__(ref, pins, sim)
        self.units, self.q = units, [0] * len(units)
        self.fall, self.al = falling, active_low

    def evaluate(self):
        for i, u in enumerate(self.units):
            s = self.inp(u["s"]) if u.get("s") else (1 if self.al else 0)
            r = self.inp(u["r"]) if u.get("r") else (1 if self.al else 0)
            if self.al:
                s, r = NOT(s), NOT(r)
            if s == 1 or r == 1:
                self.q[i] = 1 if s == 1 else 0
            elif (self.falling(u["cp"]) if self.fall else self.rising(u["cp"])):
                j, k = self.prev.get(u["j"], self.inp(u["j"])), self.prev.get(u["k"], self.inp(u["k"]))
                q = self.q[i]
                if j == 1 and k == 1: q = NOT(q)
                elif j == 1: q = 1
                elif k == 1: q = 0
                self.q[i] = q
            self.drive(u["q"], self.q[i]); self.drive(u["qn"], NOT(self.q[i]))

    def init(self):
        for i, u in enumerate(self.units):
            self.drive(u["q"], self.q[i]); self.drive(u["qn"], NOT(self.q[i]))


class UpDown19x(Device):
    """74LS192 (BCD) / 74LS193 (binary): CPU/CPD rising edges, PL low = async load,
    MR high = async clear, TCU/TCD active low."""

    def __init__(self, ref, pins, sim, modulus):
        super().__init__(ref, pins, sim)
        self.m, self.c = modulus, 0

    def evaluate(self):
        if self.inp("MR") == 1:
            self.c = 0
        elif self.inp("PL") == 0:
            d = [self.inp(n) for n in ("D0", "D1", "D2", "D3")]
            if X not in d:
                self.c = d[0] + 2 * d[1] + 4 * d[2] + 8 * d[3]
        else:
            if self.rising("CPU") and self.inp("CPD") == 1:
                self.c = (self.c + 1) % self.m
            elif self.rising("CPD") and self.inp("CPU") == 1:
                self.c = (self.c - 1) % self.m
        self.out()

    def out(self):
        for i in range(4):
            self.drive(f"Q{i}", (self.c >> i) & 1)
        self.drive("TCU", 0 if (self.c == self.m - 1 and self.inp("CPU") == 0) else 1)
        self.drive("TCD", 0 if (self.c == 0 and self.inp("CPD") == 0) else 1)

    init = out


class Sync16x(Device):
    """74LS160/161 (async MR), 162/163 (sync MR); PE low = sync load; CEP&CET enable."""

    def __init__(self, ref, pins, sim, modulus, sync_reset):
        super().__init__(ref, pins, sim)
        self.m, self.sync, self.c = modulus, sync_reset, 0

    def evaluate(self):
        if not self.sync and self.inp("MR") == 0:
            self.c = 0
        elif self.rising("CP"):
            if self.sync and self.prev.get("MR") == 0:
                self.c = 0
            elif self.prev.get("PE") == 0:
                d = [self.prev.get(f"D{i}") for i in range(4)]
                if X not in d:
                    self.c = sum(b << i for i, b in enumerate(d))
            elif self.prev.get("CEP") == 1 and self.prev.get("CET") == 1:
                self.c = (self.c + 1) % self.m
        self.out()

    def out(self):
        for i in range(4):
            self.drive(f"Q{i}", (self.c >> i) & 1)
        self.drive("TC", 1 if (self.c == self.m - 1 and self.inp("CET") == 1) else 0)

    init = out


class BCD7Seg(Device):
    """74LS47 (active-low outputs) / 74LS48 / 4511 (active-high)."""
    SEG = {0: "abcdef", 1: "bc", 2: "abdeg", 3: "abcdg", 4: "bcfg", 5: "acdfg", 6: "acdefg",
           7: "abc", 8: "abcdefg", 9: "abcdfg"}

    def __init__(self, ref, pins, sim, active_low):
        super().__init__(ref, pins, sim)
        self.al = active_low
        self.names = [n for n in pins]

    def evaluate(self):
        names = ("A0", "A1", "A2", "A3") if "A0" in self.pins else ("D0", "D1", "D2", "D3")
        a = [self.inp(n) for n in names]
        if X in a:
            v = None
        else:
            v = a[0] + 2 * a[1] + 4 * a[2] + 8 * a[3]
        lit = self.SEG.get(v, "")
        for s in "abcdefg":
            on = 1 if s in lit else 0
            self.drive(s, NOT(on) if self.al else on)

    init = evaluate


class PISO165(Device):
    """74LS165 parallel/serial-in, serial-out.  PL low = asynchronous parallel load
    (stage i = Di).  Otherwise the internal clock is CP OR CE: on its rising edge
    stage0 <- DS, stage i <- stage i-1.  Outputs Q7 = stage7 and Q7N = /stage7,
    so after a load Q7 already shows D7 and the byte leaves MSB first."""

    def __init__(self, ref, pins, sim):
        super().__init__(ref, pins, sim)
        self.s = [X] * 8

    def evaluate(self):
        if self.inp("PL") == 0:
            self.s = [self.inp(f"D{i}") for i in range(8)]
        else:
            old = OR([self.prev.get("CP", X), self.prev.get("CE", X)])
            new = OR([self.inp("CP"), self.inp("CE")])
            if old == 0 and new == 1:
                self.s = [self.prev.get("DS", X)] + self.s[:7]
        self.out()

    def out(self):
        self.drive("Q7", self.s[7])
        self.drive("Q7N", NOT(self.s[7]))

    init = out


class SIPO164(Device):
    """74LS164 serial-in, parallel-out.  MR low = asynchronous clear.  Rising CP:
    Q0 <- Dsa AND Dsb, Qi <- Qi-1.  After 8 clocks Q7 holds the first bit sent."""

    def __init__(self, ref, pins, sim):
        super().__init__(ref, pins, sim)
        self.s = [X] * 8

    def evaluate(self):
        if self.inp("MR") == 0:
            self.s = [0] * 8
        elif self.rising("CP"):
            self.s = [AND([self.prev.get("Dsa", X), self.prev.get("Dsb", X)])] + self.s[:7]
        self.out()

    def out(self):
        for i in range(8):
            self.drive(f"Q{i}", self.s[i])

    init = out


class HexKey(Device):
    """CircuitMaker 'Hex Key' keypad: outputs the stored key value on pins 1..4
    (pin 1 = bit 0, the LSB -- the same convention as the Hex Display)."""

    def __init__(self, ref, pins, sim, value):
        super().__init__(ref, pins, sim)
        self.value = value

    def init(self):
        for i in range(4):
            self.drive(str(i + 1), (self.value >> i) & 1, delay=0)

    def set(self, v):
        self.value = v
        self.init()


class Pulser(Device):
    def __init__(self, ref, pins, sim, high, low, start_high=False):
        super().__init__(ref, pins, sim)
        self.high, self.low, self.start_high = high, low, start_high
        self.enabled = True

    def init(self):
        v = 1 if self.start_high else 0
        self._set(v, 0)

    def _set(self, v, t):
        self.drive("Q1", v, delay=t)
        self.drive("Q2", NOT(v), delay=t)
        nxt = self.high if v == 1 else self.low
        self.sim.at(self.sim.now + t + nxt, lambda: self._set(NOT(v), 0) if self.enabled else None)


class Const(Device):
    def __init__(self, ref, pins, sim, value, pin="1"):
        super().__init__(ref, pins, sim)
        self.value, self.pin = value, pin

    def init(self):
        self.drive(self.pin, self.value, delay=0)

    def set(self, v):
        self.value = v
        self.drive(self.pin, v, delay=0)


class Sink(Device):
    pass


# ------------------------------------------------------------------- the model map
GATE_RE = re.compile(r"(\d)-In(?:put)? (AND|NAND|OR|NOR|XOR|XNOR)")
CMOS_GATES = {"4011": ("NAND", 2), "4001": ("NOR", 2), "4081": ("AND", 2), "4071": ("OR", 2),
              "4069": ("NOT", 1), "4070": ("XOR", 2), "4077": ("XNOR", 2), "4093": ("NAND", 2),
              "4023": ("NAND", 3), "4025": ("NOR", 3), "4073": ("AND", 3), "4075": ("OR", 3),
              "4012": ("NAND", 4), "4002": ("NOR", 4), "4082": ("AND", 4), "4072": ("OR", 4),
              "4049": ("NOT", 1), "4050": ("BUF", 1)}


def make_device(sim, ref, comp, pinnets):
    name, model = comp.name.rstrip("~"), (comp.label or "").upper()
    m = GATE_RE.match(name)
    if m:
        return Gate(ref, pinnets, sim, m.group(2), int(m.group(1)))
    base = name.split(":")[0]
    if base in ("Inverter",):
        return Gate(ref, pinnets, sim, "NOT", 1)
    if base in ("Buffer",):
        return Gate(ref, pinnets, sim, "BUF", 1)
    if base in CMOS_GATES:
        f, n = CMOS_GATES[base]
        return Gate(ref, pinnets, sim, f, n)
    num = re.sub(r"^74[A-Z]*", "74", name)
    if num in ("7490", "7492", "7493"):
        return Ripple90(ref, pinnets, sim, num[-2:])
    if num == "7474":
        units = [dict(cp=f"CP{i}", d=f"D{i}", s=f"S{i}", r=f"R{i}", q=f"Q{i}", qn=f"{{Q{i}}}") for i in (1, 2)]
        return DFF(ref, pinnets, sim, units, active_low=True)
    if num in ("7476", "74112", "7473", "74107"):
        units = [dict(j=f"J{i}", k=f"K{i}", cp=f"CP{i}", s=f"SD{i}" if f"SD{i}" in pinnets else None,
                      r=f"RD{i}" if f"RD{i}" in pinnets else f"R{i}" if f"R{i}" in pinnets else None,
                      q=f"Q{i}", qn=f"{{Q{i}}}") for i in (1, 2)]
        return JKFF(ref, pinnets, sim, units, falling=True, active_low=True)
    if num in ("74192", "74193"):
        return UpDown19x(ref, pinnets, sim, 10 if num == "74192" else 16)
    if num in ("74160", "74161", "74162", "74163"):
        return Sync16x(ref, pinnets, sim, 10 if num in ("74160", "74162") else 16, num in ("74162", "74163"))
    if num == "74165":
        return PISO165(ref, pinnets, sim)
    if num == "74164":
        return SIPO164(ref, pinnets, sim)
    if comp.name == "Hex Key~":
        return HexKey(ref, pinnets, sim, comp.nets[10] if len(comp.nets) > 10 else 0)
    if num in ("7447", "7448") or base == "4511":
        return BCD7Seg(ref, pinnets, sim, active_low=(num == "7447"))
    if base == "4013":                     # one D flip-flop per part, active-HIGH S/R, rising edge
        return DFF(ref, pinnets, sim, [dict(cp="CP", d="D", s="S", r="R", q="Q", qn="{Q}")], active_low=False)
    if base == "4027":                     # JK, rising edge, active-HIGH S/R
        return JKFF(ref, pinnets, sim, [dict(j="J", k="K", cp="CP", s="S", r="R", q="Q", qn="{Q}")],
                    falling=False, active_low=False)
    if comp.name == "Pulser~":
        return Pulser(ref, pinnets, sim, high=max(1, comp.nets[8]), low=max(1, comp.nets[7]),
                      start_high=sim.pulser_start_high)
    if comp.name == "Logic Switch~":       # CM keeps the state in nets[10] (1 = on); the label is text
        return Const(ref, pinnets, sim, 1 if len(comp.nets) > 10 and comp.nets[10] == 1 else 0)
    if comp.name == "+V":                  # a DC supply: its value is the label ("+5V", "-12V")
        m = re.search(r"[-+]?\d+(\.\d+)?", comp.label or "")
        v = float(m.group(0)) if m else 5.0
        return Const(ref, pinnets, sim, 1 if v >= 2.0 else 0 if v <= 0.8 else X)
    if comp.name == "Ground~":
        return Const(ref, pinnets, sim, 0)
    if comp.name in ("Logic Display~", "Hex Display~", "SCOPE", "CC 7-Seg~", "CA 7-Seg~"):
        return Sink(ref, pinnets, sim)
    if "digital" in (comp.dtype or "") or re.match(r"^(74|40|45)", name):
        raise NotImplementedError(f"{ref}: no logic model for {comp.name!r} ({comp.label}); add one to logicsim.py")
    return Sink(ref, pinnets, sim)       # analog/passive parts are ignored


# ------------------------------------------------------------------- simulator
class Sim:
    def __init__(self):
        self.now = 0
        self.q = []
        self.seq = 0
        self.values = {}
        self.fanout = defaultdict(list)
        self.devices = {}
        self.drivers = defaultdict(dict)      # net -> {device: value}
        self.net_of = {}
        self.history = defaultdict(list)       # net -> [(t, v)]
        self.cmos_inputs = set()
        self.delays = {}
        self.random_delays = None
        self.pulser_start_high = False
        self.pending_actions = []
        self._fixed = {}
        self.floating = set()

    # --- construction
    @classmethod
    def load(cls, path, random_delays=None, seed=None, pulser_start_high=False):
        c = ckt.Circuit.load(path)
        sim = cls()
        sim.pulser_start_high = pulser_start_high
        if random_delays:
            sim.random_delays = random.Random(seed)
            sim.rand_range = random_delays
        nets = ckt.build_nets(c)
        pin2net = {}
        for k, n in enumerate(nets):
            for dp in n:
                pin2net[dp] = k
        syms = devlib.load()
        for i, comp in enumerate(c.components, 1):
            s = syms.get(comp.name)
            if not s:
                continue
            ref = comp.designator or f"#{i}"
            pinnets = {}
            seen = defaultdict(int)
            for p in s.pins:
                nm = p.name or str(p.index)
                if nm in ("I",):                    # gate inputs are all called "I"
                    seen[nm] += 1
                    nm = f"I{seen['I']}"
                net = pin2net.get((i, p.index))
                if net is None:                      # unconnected pin: private net
                    net = ("open", i, p.index)
                    sim.floating.add(net)
                pinnets[nm] = net
                sim.net_of[f"{ref}.{nm}"] = pinnets[nm]
                sim.net_of[f"{ref}.{p.index}"] = pinnets[nm]
            dev = make_device(sim, ref, comp, pinnets)
            sim.devices[ref] = dev
            if comp.name.startswith("40") or (comp.label or "").startswith("40"):
                dev.cmos = True
            for nm, net in pinnets.items():
                if net is not None:
                    sim.fanout[net].append(dev)
        sim.circuit = c
        for d in sim.devices.values():
            d.snapshot()
        for d in sim.devices.values():
            d.init()
        sim.run(0)
        return sim

    # --- values
    def read(self, net, dev=None):
        """Undriven unconnected pins read like real inputs: TTL floats HIGH, CMOS is X.
        A driven one (an output with nothing attached) reads its driven value."""
        if net is None or (net in self.floating and not self.drivers.get(net)):
            return X if getattr(dev, "cmos", False) else 1
        return self.values.get(net, X)

    def get(self, sig):
        net = self.net_of[sig]
        return self.read(net)

    def delay_for(self, dev, d, key=None):
        """Delay of one output path.  With --sweep each (device, path) gets a fixed
        random delay for the whole run (a real chip has fixed but unknown delays;
        re-drawing per event would reorder events and invent glitches)."""
        if d is not None:
            return d
        if self.random_delays:
            k = (dev.ref, key)
            if k not in self._fixed:
                lo, hi = self.rand_range
                self._fixed[k] = self.random_delays.randint(lo, hi)
            return self._fixed[k]
        return self.delays.get(dev.ref, dev.delay)

    def schedule(self, net, dev, value, delay):
        self.seq += 1
        heapq.heappush(self.q, (self.now + delay, self.seq, "net", (net, dev, value)))

    def at(self, t, fn):
        self.seq += 1
        heapq.heappush(self.q, (t, self.seq, "fn", fn))

    def _resolve(self, net):
        vals = set(self.drivers[net].values())
        if not vals:
            return X
        if len(vals) == 1:
            return vals.pop()
        return X                                   # contention

    def run(self, ticks):
        end = self.now + ticks
        while self.q and self.q[0][0] <= end:
            t = self.q[0][0]
            self.now = t
            changed = set()
            while self.q and self.q[0][0] == t:
                _, _, kind, payload = heapq.heappop(self.q)
                if kind == "fn":
                    payload()
                    continue
                net, dev, value = payload
                self.drivers[net][dev] = value
                nv = self._resolve(net)
                if self.values.get(net, X) != nv or net not in self.values:
                    self.values[net] = nv
                    self.history[net].append((t, nv))
                    changed.add(net)
            if changed:
                devs = []
                for n in changed:
                    for d in self.fanout[n]:
                        if d not in devs:
                            devs.append(d)
                for d in devs:
                    d.evaluate()
                for d in devs:
                    d.snapshot()
        self.now = end

    # --- test-bench helpers
    def set(self, ref, value):
        self.devices[ref].set(value)
        self.run(0)

    def bcd(self, ref):
        bits = [self.get(f"{ref}.Q{i}") for i in range(4)]
        if X in bits:
            return None
        return bits[0] + 2 * bits[1] + 4 * bits[2] + 8 * bits[3]

    def hexdisp(self, ref):
        """Digit shown by a Hex Display (pin 1 = LSB); '?' if any input is X."""
        bits = [self.get(f"{ref}.{i}") for i in range(1, 5)]
        if X in bits:
            return "?"
        return "0123456789ABCDEF"[bits[0] + 2 * bits[1] + 4 * bits[2] + 8 * bits[3]]

    def edges(self, sig, t0=0, t1=None, kind="rise"):
        net = self.net_of[sig]
        out = []
        last = None
        for t, v in self.history[net]:
            if last is not None and (t1 is None or t <= t1) and t >= t0:
                if kind == "rise" and last == 0 and v == 1:
                    out.append(t)
                if kind == "fall" and last == 1 and v == 0:
                    out.append(t)
            last = v
        return out


def _cli(argv=None):
    ap = argparse.ArgumentParser(description="digital logic simulation of a .CKT")
    ap.add_argument("file")
    ap.add_argument("--clock", help="Pulser designator driving the circuit")
    ap.add_argument("--pulses", type=int, default=50)
    ap.add_argument("--bcd", help="comma list of BCD counters, most significant first, e.g. U2,U1")
    ap.add_argument("--freq", help="signal whose period (in clock pulses) is measured, e.g. U2.Q0")
    ap.add_argument("--script", default="", help="'N:REF=V,...' set switch REF to V after clock pulse N")
    ap.add_argument("--sweep", type=int, default=0, help="repeat N times with random delays 1..4")
    ap.add_argument("--show", type=int, default=60, help="print at most this many states")
    ap.add_argument("--sweep-half-period", type=int, default=50,
                    help="Pulser high/low used during --sweep runs (ticks)")
    a = ap.parse_args(argv)
    script = defaultdict(list)
    for item in filter(None, a.script.split(",")):
        n, rest = item.split(":")
        ref, v = rest.split("=")
        script[int(n)].append((ref, int(v)))
    runs = [None] + list(range(a.sweep))
    signatures = set()
    for run in runs:
        sim = Sim.load(a.file, random_delays=(1, 4) if run is not None else None, seed=run)
        clk = sim.devices[a.clock] if a.clock else None
        if isinstance(clk, Const):
            # a Logic Switch used as a manual clock: drive it as a 5+5-tick pulser
            sw = clk
            clk = Pulser(sw.ref, sw.pins, sim, high=5, low=5)
            clk.pins = {"Q1": sw.pins[sw.pin]}
            sim.devices[a.clock] = clk
            sim.drivers[sw.pins[sw.pin]].pop(sw, None)     # the switch stops driving
            clk.init()
            sim.net_of[f"{a.clock}.Q1"] = sw.pins[sw.pin]
        if clk and run is not None:
            # random delays up to 4 ticks per device: stretch the clock so every
            # ripple settles; only genuine races (pulse widths) can now differ
            clk.high = clk.low = a.sweep_half_period
        period = clk.high + clk.low if clk else 10
        seq = []
        clk_sig = f"{a.clock}.Q1"
        t_start = 0

        def read():
            vals = [sim.bcd(r) for r in a.bcd.split(",")]
            return "".join("?" if v is None else str(v) if v < 10 else f"[{v}]" for v in vals)
        if a.bcd:
            seq.append(read())                      # state before the first pulse
        n = 0
        guard = 0
        while n < a.pulses and guard < (a.pulses + 5) * period * 4:
            before = len(sim.edges(clk_sig, kind="fall"))
            sim.run(1)
            guard += 1
            if len(sim.edges(clk_sig, kind="fall")) > before:
                n += 1
                sim.run(max(1, period - 2))         # sample just before the next edge
                if a.bcd:
                    seq.append(read())
                for ref, v in script.get(n, []):    # switch actions happen after sampling
                    sim.set(ref, v)
                    sim.run(1)
                guard = 0
        sig = tuple(seq)
        signatures.add(sig)
        if run is None:
            if a.bcd:
                print(f"display before pulse 1, then after each falling clock edge (1..{a.pulses}):")
                for i in range(0, min(len(seq), a.show), 20):
                    print(f"  [{i:3d}] " + " ".join(seq[i:i + 20]))
                if len(seq) > a.show:
                    print(f"  ... last: {' '.join(seq[-10:])}")
            if a.freq:
                fin = sim.edges(clk_sig, t_start, kind="rise")
                fo = sim.edges(a.freq, t_start, kind="rise")
                if len(fo) >= 2:
                    per = [b - a_ for a_, b in zip(fo, fo[1:])]
                    ratio = [len([t for t in fin if x <= t < y]) for x, y in zip(fo, fo[1:])]
                    print(f"{a.freq}: {len(fo)} rising edges; period(ticks)={sorted(set(per))}; "
                          f"input clocks per output period={sorted(set(ratio))}")
                    hi = []
                    hist = sim.history[sim.net_of[a.freq]]
                    for (t0, v0), (t1, v1) in zip(hist, hist[1:]):
                        if v0 == 1 and t0 >= fo[0]:
                            hi.append(t1 - t0)
                    if hi:
                        print(f"   high time(ticks)={sorted(set(hi))} (duty ~ {hi[0] / per[0]:.2f})")
                else:
                    print(f"{a.freq}: fewer than 2 rising edges")
    if a.sweep:
        print(f"delay sweep: {len(runs)} runs, {len(signatures)} distinct display sequence(s)"
              + ("  -> RACE/DELAY DEPENDENT" if len(signatures) > 1 else "  -> robust"))
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
