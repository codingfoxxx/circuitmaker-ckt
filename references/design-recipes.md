# Digital design recipes for CircuitMaker exercises (with the traps)

Each recipe lists the logic, how to draw it with `build.py`, and what to check.

## 74LS90 (7490) basics
- Two sections: A (÷2: CP0 → Q0) and B/C/D (÷5: CP1 → Q1 Q2 Q3). Both clock on the **falling** edge.
- BCD 0–9: clock on CP0, **Q0 → CP1**. Output weights Q3 Q2 Q1 Q0 = 8 4 2 1.
- Bi-quinary (symmetric ÷10 on Q0): clock on CP1, Q3 → CP0.
- MR1·MR2 = 1 → 0000. MS1·MS2 = 1 → 1001 (has priority). Unused MS/MR **must** go to Ground. Floating TTL
  inputs are HIGH, so an open MS presets to 9 and an open MR holds the reset.
- Cascading decades: tens CP0 ← units **Q3**, which falls exactly on 9 → 0.
- Internally the ÷5 ripples: on 3→4, Q1 falls *before* Q2 rises, so no "6" glitch. On 7→8 the transient
  0110 (6) does occur. A decode of 6/46 is safe only when no 7→8 step happens while the other decode bits
  are true. `logicsim.py` models this structure; `--sweep` exposes such races.

## Mod-N counter with asynchronous reset (7490, 7493, 7492, 74192/193)
1. Write N in binary (BCD per digit). Decode only the **1** bits of N that are first all-1 at N among the
   reachable states 0..N−1. Example: 46 → tens Q2, units Q2, units Q1. 6 → Q2, Q1.
2. Feed the decode to MR. The 7490's MR is already MR1·MR2, so a 2-bit decode needs no gate (Q1→MR2,
   Q2→MR1). A 3-bit decode needs one AND (74LS11, or AND2 + the internal AND).
3. Reset **all** cascaded chips with the same signal.
4. State N exists only for a few gate delays. Statements like "ao 46º pulso volta a 0" mean exactly this.

## Frequency divider ÷M
- ÷12 with two 7490s: ÷6 (BCD reset at 6) followed by ÷2 (flip-flop A clocked by the ÷6's Q2, which falls
  once per 6 inputs). This gives a 50 % duty output. The reverse order (÷2 then ÷6, output Q2) gives
  1/3 duty. Both are correct ÷12.
- ÷10: one 7490 in BCD (Q3, 20 % duty) or bi-quinary (Q0, 50 %).
- Digital-mode time is in ticks. State "IN period T → OUT period 12T". Do not claim measured Hz.
  Verify with `logicsim.py --freq OUT` and `cm_verify.py wave` (count input edges per output period).

## Stop at N until reset
- Decode N (bits that are first all-1 at N), invert it (NAND) to get EN, and gate the counter clock:
  `CLK_U1 = AND(CLK, EN)`. At N the count itself holds EN = 0, so the stop persists until MR. A latch is
  optional, not required.
- **Edge safety:** with falling-edge counters, AND gating is glitch-free because the count changes right
  after a falling edge while CLK is low. With rising-edge counters (74LS192/163/74), use OR gating
  `CLK_U1 = OR(CLK, /EN)` or the chip's enable input (74LS160-163 CEP/CET) instead.
- Reset: a Logic Switch (1 = reset) to MR of every chip. In the test, send pulses after N and check the
  count stays at N. Then set RST=1 (00), RST=0, and check it resumes 01, 02, …
  (`cm_verify.py count --click "60:RST,62:RST"`).

## Shift registers: 74LS165 (paralelo/série) -> 74LS164 (série/paralelo)
- 165: /PL low = asynchronous load (Q7 shows D7 at once). Shift on the rising edge of CP OR CE
  (CE = clock inhibit, tie to GND). DS enters stage 0. So the byte leaves **MSB first** on Q7.
- 164: Q0 <- Dsa·Dsb on the rising CP. /MR is an asynchronous clear (tie to +5V). After 8 clocks
  Q7..Q0 = D7..D0. Tie Dsb to Dsa (or to +5V).
- One clock net for both chips. The 164 samples the old Q7 because the 165 output changes a gate delay
  after the edge.
- Hex Keys feed the data (pin 1 = LSB). Hex Displays show Q7..Q4 (left) and Q3..Q0 (right).

## Gated clock that stops after N edges (RDY / "bloqueia o clock")
- Count the **same edge** that does the work. The registers shift on rising edges, so clock a 7493 with
  /CLKG (inverter). Then gate with **OR** (idle HIGH): `CLKG = CLK + RDY`. At the N-th rising edge CLK
  is already high, so forcing CLKG high adds no edge.
  - Counting the opposite edge with OR adds a 9th edge.
  - AND gating with a falling-edge count miscounts when LOAD is released while CLK is high.
- **Hold the clock during the load:** `CLKG = CLK + RDY + /LOAD`.
  - Without the /LOAD term, a LOAD released on a clock edge lets the 165 ignore the edge (still
    loading) while the counter, released one inverter later, counts it. The byte then arrives shifted
    1 bit (12/256 random phases in logicsim).
  - With it, the first possible edge comes after every chip has left reset.
- **Timing margin:** the stop signal must arrive while CLK is still high:
  `t_high > t(OR) + t(INV) + t(counter -> RDY)`. CM measured 5 ticks for a 7493 Q3, so use a Pulser of
  ≥ 10/10 ticks. With 5/5, logicsim produced a 9th edge.
- Verify with logicsim at random LOAD phases and lengths, plus the delay sweep. In CM, verify with SCOPE
  traces (LOAD, CLK, CLKG, SER, RDY) and a LOAD released exactly on a CLK rising-edge tick. Rising edges
  are at `t = 1 + low + k·(low+high)`.

## Synchronous counters (74LS160–163, 74LS190–193)
- 160/161 have asynchronous MR (active low). 162/163 have synchronous MR, so decode N−1 for mod-N.
- PE (active low) loads D0..D3 on the clock: load-based mod-N loads the start value at the terminal count.
- 192/193: CPU/CPD rising edge with the other input held HIGH. PL is an asynchronous load (active low),
  MR an asynchronous clear (active high). TCU/TCD are active-low borrow/carry for cascading.

## Displays and observation
- `Hex Display`: 4 inputs; pin "1" = LSB (bit 0) … "4" = MSB. Put the tens left of the units. Fan the
  outputs right to columns, then up to the display.
- `Logic Display`: one LED per signal. `SCOPE`: named trace in the digital waveform window, needed for
  `cm_verify.py wave`.
- `Pulser`: automatic clock, `high`/`low` in ticks. `Logic Switch`: manual 0/1, toggled by clicking.
  Its initial state is `on=` (stored in a[10]), not the label.
- `+V` must say `+5V` (build.py does it; the stock template is -12V).

## Pre-delivery checklist
- [ ] Every input of every IC connected (validate.py: 0 errors), MS/MR/PL/CLR/EN deliberately tied
- [ ] No warnings left (wire over pin tip, overlaps, wires through bodies)
- [ ] logicsim nominal sequence matches the statement, and `--sweep` reports robust
- [ ] CM opens it with no popups and its nets equal ours
- [ ] CM simulation shows the required behaviour (count / waveform), captured as text, not eyeballed
- [ ] VALIDACAO.md says what was verified and what was not (e.g. Hz vs ticks)
