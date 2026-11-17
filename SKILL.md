---
name: circuitmaker-ckt
description: Create, read, edit, validate and simulate CircuitMaker 2000 (.CKT) schematics on this Windows PC - the old Protel/MicroCode CircuitMaker, not Altium's modern CircuitMaker. Covers digital-electronics exercises end to end - counters (7490/74LS90, 7493, 7492, 74192/193, 74160-163), frequency dividers, mod-N and stop-at-N counters, shift registers and serial links (74165/74164, paralelo/série, série/paralelo, RDY, clock gating), flip-flops, gates (74xx/40xx such as 7400/4011), BCD/hex displays, Hex Key (chave hexadecimal), Pulser clocks, Logic Switches - delivering .CKT files proven to work in CircuitMaker's own simulator. Use it whenever the user mentions CircuitMaker, a .CKT file, "esquemático", "simular no CircuitMaker", or pastes an Eletrônica Digital exercise (contador, divisor de frequência, CI 7490, "projete um circuito...") that must be drawn or delivered as a schematic, even if CircuitMaker is not named explicitly.
---

# CircuitMaker 2000 `.CKT`: design, generate, prove

Toolkit for producing CircuitMaker 2000 schematics that are **verified, not just drawn**. Every claim
"it works" must come from a simulation, not from "the file opened". Opening without errors proves
nothing about the logic.

Scripts live in `scripts/` (Python 3 + pywin32 + Pillow, Windows). CircuitMaker is expected at
`C:\Program Files\CircuitMaker 2000`; override it with the env var `CM2000_DIR`. Run the scripts from
`scripts/` (they import each other). On first use run `python check_setup.py`; if it reports something
MISSING, fix that (e.g. `pip install pywin32 pillow`) before building anything.

| script | what it does |
|---|---|
| `ckt.py` | lossless parser/writer (byte-identical round trip on all shipped examples), netlist from wire metadata. `python ckt.py inspect F.CKT` |
| `devlib.py` | pin names/positions of every symbol in `Device.lib`; model resolution `74LS32 -> 2-In OR~ + DIP pinout`. `python devlib.py show 74LS90` |
| `build.py` | `Schematic` API: place parts, wire pins by name, T-junctions, notes; `Schematic.load()` to edit existing files |
| `validate.py` | static checks: floating inputs, output contention, MS/MR misuse, drawing vs connection mismatches, wires over pins/bodies |
| `logicsim.py` | event-driven 0/1/X simulator using CM's exact connectivity; `--sweep N` re-runs with random fixed delays to expose races |
| `cmgui.py` | drives cirmaker.exe through window messages: open, save-as, digital step, waveform export, screenshots |
| `cm_verify.py` | proof inside CircuitMaker: `nets` (CM's netlist == ours, no popups), `count` (reads Hex Displays after every clock pulse), `wave` (per-tick probe traces) |
| `wavewmf.py` | decodes CM's digital-waveform WMF export into samples |
| `harvest_templates.py` | adds component records from a CM-saved file to `assets/templates.json` |
| `wavepng.py` | timing diagram (PNG) from per-tick traces; `cm_verify.py wave F.CKT waves.png` calls it |
| `check_setup.py` | checks Python, pywin32, Pillow, the CircuitMaker install and `Device.lib`. Run it first on a new machine |

## Workflow (do all steps; report honestly which ones ran)

1. **Design on paper first.** Write the state sequence and decide which states to decode, and check
   that the decode is not satisfied earlier. List the reachable states, and list the transient states that
   ripple counters produce. Check the clock edge (7490/7493/7492 count on the **falling** edge of CP0/CP1)
   and the reset polarity. `references/design-recipes.md` has the standard solutions and their traps.
2. **Generate** with `build.py` (see "Building: minimal example" below; write one small builder script per
   circuit and keep it next to the output so the file can be regenerated). Place parts unrotated.
   Choose coordinates so each net has its own vertical/horizontal track. Wires must leave pins outward (the builder adds stubs).
   Put a hex display or SCOPE probe on what the user must observe. The tens digit goes left of the units.
3. **`python validate.py out.CKT`** must give 0 errors. Treat warnings as drawing bugs (a wire over a pin
   tip *looks* connected and misleads the reader); fix them unless you can explain each one.
4. **`python logicsim.py out.CKT --clock CLK --pulses N --bcd TENS,UNITS [--script "n:SW=1,..."] --sweep 40`**
   gives the expected sequence. The sweep must say `robust`. For dividers, use `--freq REF.PIN` (period in
   input clocks + duty).
5. **Prove it in CircuitMaker** (ask before taking over the user's CircuitMaker window; a running
   instance may hold unsaved work):
   - `python cm_verify.py nets out.CKT` → `"popups": []`, `"nets_equal": true`
   - counters: `python cm_verify.py count out.CKT --pulses N --displays DEZ,UNI [--click "60:RST,62:RST"]`
   - waveforms: put SCOPE probes in the circuit, then `python cm_verify.py wave out.CKT waves.png --ticks 600`
     (prints the per-tick traces and saves a timing diagram)
6. **Deliver** the `.CKT` files, screenshots (`cm_verify.py shot`) and a short `VALIDACAO.md` that states
   what each check showed and what was *not* verified. Copy the files somewhere the user works. Never write
   into `C:\Program Files\CircuitMaker 2000`: Windows silently redirects the write to
   `%LOCALAPPDATA%\VirtualStore\...`, and CM then reads a different copy than the one you edit.

## Facts that decide correctness (all verified in this install)

- **Connectivity = wire metadata, not drawing.** Each wire stores `pinA pinB … devA devB wireA wireB`.
  CM connects exactly what the metadata says. A wire drawn onto a pin without metadata is *not* connected,
  and a wire with metadata stays connected even if drawn far away. So geometry only matters for the human
  reader, which is why `validate.py` checks both.
- **Ground symbols form one global net** (pin attribute `GND;`). `+V` is global `VCC;`.
- Pin tip = pin base + 6 px outward, **+6 more if the pin has an inversion bubble** (74LS90 CP0/CP1, NAND
  outputs, active-low inputs). A previous generator ended wires on the bubble; CM still connected them,
  but the drawing showed a gap.
- 74LS90 symbol order: `MS1 MS2 MR1 MR2 CP0 CP1 | Q3 Q2 Q1 Q0` (DIP 6,7,2,3,14,1 | 11,8,9,12).
  MR = MR1·MR2 resets to 0. MS = MS1·MS2 presets to **9** and has priority. **Unconnected TTL inputs
  float HIGH**, so leaving MS or MR open presets or holds the counter. Tie unused MS/MR to Ground.
- Pulser record: `nets[7]` = **Pulse Low** ticks, `nets[8]` = **Pulse High** ticks (checked in CM's
  Edit Pulser dialog; the opposite guess is wrong). After Reset its output is low for `low+1` ticks, so
  the k-th falling edge is at `t = 1 + k·(high+low)`.
- Digital mode measures time in **ticks**, not seconds. A Pulser labelled "12 kHz" is a convention. What the
  simulation proves is the frequency *ratio*. Say this explicitly when a statement gives Hz.
- **Logic Switch state lives in `nets[10]`** (ON = 11-entry array with `nets[10] = 1`). The label
  `0V`/`5V` is only text that CM ignores at Reset. An earlier guess (label + `l8[6]`) left a switch
  marked "5V" at 0 in CM. Use `add(..., on=True)` and confirm with `logic_at` in CM. In simulation a
  mouse click toggles it.
- **`+V` is a DC source whose value is its label.** The stock template says `-12V`. build.py writes `+5V`,
  and validate.py rejects anything that is not a logic 1.
- **Hex Key** (chave hexa): value in `nets[10]` (`add(..., value=0xA)`). Pin 1 = LSB, the same as the Hex
  Display. In CM, click it, then type the digit. The Hex Display shows B and D as lowercase `b`/`d`.
- A Logic Switch is a manual input, not a clock. Use the Pulser for automatic clocks.
- CM reorders components when it saves, so compare netlists by designator, not by index.
- Gated clocks: count the same edge that does the work, gate so the stop lands on the level the clock
  already has, hold the clock during asynchronous loads, and make the clock high time longer than the
  counter's delay to its stop output. Recipe and proof: `references/design-recipes.md`.
- CM screenshots and display reads use `screen_canvas()` (screen grab). PrintWindow returns half-drawn
  canvases during simulation.

Details: `references/ckt-format.md` (file grammar), `references/circuitmaker-gui.md` (automation,
simulator timing, colors, pitfalls), `references/devices-pins.md` (generated pin table of ~50 common
parts), `references/design-recipes.md`.

## Building: minimal example

```python
import sys; sys.path.insert(0, r"C:\Users\<user>\.claude\skills\circuitmaker-ckt\scripts")
from build import Schematic
s = Schematic()
s.text("Contador mod-6", 40, 30)
s.add("Pulser", "CLK", 110, 327, high=5, low=5)
s.add("74LS90", "U1", 300, 300)                  # model names resolve via Device.lib/SEARCHDB
s.add("Ground", "G1", 224, 294)
s.wire("CLK.Q1", "U1.CP0")                       # same y -> straight line
q0 = s.wire("U1.Q0", "U1.CP1", via=[(348, 327), (348, 345), (252, 345), (252, 327)])
w = s.wire("U1.Q2", "U1.MR1", via=[(344, 291), (344, 238), (254, 238), (254, 291)])
s.wire("U1.Q1", "U1.MR2", via=[(350, 309), (350, 232), (248, 232), (248, 300)])
s.net("U1.MS1", "U1.MS2", "G1.1", trunk_x=224)   # trunk + T-junction branches
s.add("74LS08", "U2A", 500, 400)                  # multi-gate ICs: designator ends with the part letter
s.branch("U2A.I1", w, point=(344, 260), via=[(420, 391), (420, 260)])  # join an existing wire
s.branch("U2A.I2", q0, point=(348, 336), via=[(430, 409), (430, 336)])
s.add("Logic Display", "S5", 560, 360)           # lights at count 5 (Q2 AND Q0)
s.wire("U2A.O", "S5.1", via=[(560, 400)])
s.save(r"C:\work\mod6.CKT")
```
Pin names come from `python devlib.py show <symbol>`. Gate inputs are `I1`, `I2`, … and outputs `O`.
`Schematic.load(path)` opens an existing file; `remove(ref)`, `remove_wire(i)`, `wire_at(x, y)` and
`wires_of("U1.Q0")` support "modifique seu projeto" edits.

When `add()` reports no app-saved template for a symbol, the builder synthesizes one from a similar
record. Confirm it with `cm_verify.py nets`, then make it permanent: open/resave in CM and
`python harvest_templates.py resaved.CKT`.

## Reading / fixing a user's `.CKT`

`python ckt.py inspect F.CKT` and `python validate.py F.CKT` give the netlist with pin names and every
electrical/drawing defect. Files from other generators often have wires ending on bubbles, preset pins
left open, and overlapping wires of different nets, which look connected on screen. Explain the defects
in terms of the netlist, then fix them with `Schematic.load`.

## When CircuitMaker cannot be used

If the GUI is unavailable (no permission, not installed, user busy), deliver after steps 1–4 and say
plainly that the in-CM proof (step 5) did not run. Never call a circuit "validado no CircuitMaker"
without `cm_verify` output.
