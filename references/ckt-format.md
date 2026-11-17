# CircuitMaker 2000 `.CKT` text format (as written by CM 2000 SP1, version line "5.6")

Everything below was reverse-engineered from the 82 example files shipped with CircuitMaker, plus files
re-saved by the installed program. Each fact is marked with how it was checked. `ckt.py` implements it
and round-trips 85/85 structured files byte for byte. The 3 ancient "version 4" examples are kept
verbatim, not parsed.

## Encoding
- Written by the C runtime in text mode: every `\n` in memory is `\r\n` on disk. So strings containing
  `\r\n` (multi-line SPICE templates, notes) appear as `\r\r\n` on disk. Decode with
  `bytes.replace(b"\r\n", b"\n").decode("latin-1")`, then encode by reversing that. Do not normalize
  line endings any other way. *(round trip)*
- Strings are length-prefixed: `"<n> <n chars>"`. The empty string is `"0"`. `n` counts the in-memory
  form (`\r\n` inside = 2 chars). *(round trip)*
- Integer arrays are wrapped 10 per line.
- Floats use `%g` (`1e+09`, `46338.8`). Some footer doubles are uninitialized memory (`5.06792e-315`).
  Copy the footer, don't interpret it.

## Top level
```
CircuitMaker Text
5.6
Probes: N            (5.6 only) followed by 3 lines per probe: node, analysis name, "x y z color"
<header lines ...>   colors, window rects, title-block strings; ends with 5 strings
<C>                  component count
<C component records>
<W>                  wire count
<W wire records>
<T>                  text-note count
<T notes>
<footer>             analysis/scope settings; copy from an app-saved file
```
- Header line 11 (`0 0 30 100 10`): field 3 is the Digital panel **Speed**. *(edited the file, CM's
  panel showed the new value)* Step Size and X Magnification are **not** stored there.
- The parser finds the component count by trying candidate lines and keeping the first parse that yields
  components (the header length varies by version).

## Component record
```
name                         str   symbol name in Device.lib: "74LS90", "4011~", "2-In AND~", "Logic Switch~"
symid x y mirror n m         ints  symid = symbol id (219 is shared by many); (x,y) = origin on the sheet
a[0..n]                      ints  a[1..npins] = net id per pin (CM recomputes; write 0). Extra entries hold
                                   parameters: Pulser a[7]=LOW ticks, a[8]=HIGH ticks; Signal Gen stores float32 bits;
                                   Logic Switch ON = 11 entries with a[10]=1 (OFF = just [0, net]);
                                   Hex Key a[10]=value 0..15, a[11]=ord of its key char ('A' = 65)
[k + k floats]               only some sources (Signal Gen PWL table) - detected when the next line is a bare int
macro                        str
f0 f1 flags orient           orient & 511 = rotation (0/90/180/270), orient & 512 = mirror; high bits may be garbage
                             flags: 0x200 shows the designator, 0x1000 shows the label/value
label     [bbox]             str (+4 ints if non-empty)  e.g. "74LS90", "0V"/"5V" for switches, "1MEG"
designator[bbox]             str (+bbox)                 "U1", "U3A"
s1        [bbox]             str (+bbox)                 rarely used (e.g. "VDD" on +V)
pinattr                      str   "DVCC=14;DGND=7;"  /  "GND;" (Ground -> global net GND)
spice                        str   XSPICE line, e.g. "%D [%14bi %7bi %1i %2i][%14bo %1o %2o %3o] %M"
s2, type, package            str   "type:digital", "DIP14"
k \n\n k+1 ints              pin map: [0, this part's DIP pins..., all parts' DIP pins..., junk]
8 ints                       [4]=number of parts, [5]=part index (A=1) for multi-gate ICs
prefix                       str   "U", "V", "DISP"
3 ints                       first = random unique id
c ; [line] ; d               "2" / "<Delphi TDateTime> <index>" / "0"
[raw data lines]             Data Sequencer memory as letters A-P (nibbles)
```
- In `Device.lib`, pin order = 1-based pin index in wires = `%1..%n` in the SPICE line. *(2072 wire ends checked)*
- Logic Switch: the **state is a[10]** (see above), verified by saving from CM before/after a click.
  The label `0V`/`5V` is only text: CM does not read it at Reset and does not
  update it on a click. Write both consistently (build.py `on=`).
- `+V` is a DC supply whose **value is the label** ("+5V", "-12V"); its only app-saved template is
  741.ckt's `Vee -12V`, so build.py always writes `+5V`. validate.py flags a +V that is not a logic 1.
- Hex Key outputs pins 1..4 = bit0..bit3 (pin 1 = LSB, like the Hex Display); verified with 16 keys
  0..F wired to 16 displays.
- Rotated parts pivot on the symbol's box center, not the origin (residual offsets per symbol). Generate
  unrotated parts.

## Wire record
```
pinA pinB net 0 0 flags 0 devA devB wireA wireB npts
x y   (npts lines)
```
- `devX` and `pinX` are 1-based. `devX = 0` means that end lands on wire `wireX` (1-based), i.e. a
  T-junction, which CM draws with a dot. The point should lie on that wire's polyline.
- `net` and `flags` are recomputed by CM on save. Write 0.
- **CM connects by this metadata only.** *(experiment: a wire ending in empty space with metadata stayed
  connected; a wire drawn onto MS2 without metadata was not connected; wrong pin metadata connected the
  wrong pin)*
- Wire end on a pin = pin tip: base + 6 px outward (+6 with a bubble). *(2072/2074 corpus ends; the
  2 misses are one odd capacitor)*

## Text note
```
-13 0 0 0 400 0 0 0 0 3 2 1 18     LOGFONT
15 Times New Roman                 face
0 0 0 <len>                        len = text length
x0 y0 x1 y1                        frame
x0+4 y0+4 x1+1 y1-2                text rect
<len> text                         may contain \r\n
```

## Device.lib (binary)
- A pin record is `int32 x, int32 y, uint16 flags, uint32 ptr, int32 side, uint16 0, int32 len, name`.
  side is one of `l r u d` (plus `x`/`n` for hidden pins). flags 43 = inversion bubble.
- The symbol name sits 266 bytes before the first pin record and its id 12 bytes before the name. A few
  tables give junk names (`digital`, `a$`); those are filtered.
- Each symbol's default device strings follow it: pinattr, SPICE, model, type, package.
- `Devicedb.dat`: `3<tree name>|<symbol>` (e.g. `37432|2-In OR`).
- `SEARCHDB.DAT` (in VirtualStore): `category \t sub \t tree name \t model \t *comment`. The comment holds
  `type: pkg: [pinattr](A:1,2,3)(B:...)`. The same comments are in `Models/*.MOD`.
