# Driving CircuitMaker 2000 (cirmaker.exe) — what works, what does not

CM 2000 is a Delphi app. `cmgui.py` drives it with window messages. Everything here was tried on this
machine (Windows 11).

## Process & windows
- Main form class `TMainForm`. When minimized, Delphi hides it and shows only `TApplication`.
  `CM.main_window()` restores it with `WM_SYSCOMMAND SC_RESTORE`.
- Schematic canvas: child class `CirmakerEdit`. Digital waveform pane: `DigitalWaveWindow`, hidden until
  Run, or until View > Split Horizontal (id 74; `show_waveforms()`).
- **Command-line file arguments are ignored.** Open files with File > Open (id 3) and the file dialog.
- The Open dialog is old-style (filename edit id 1152). Save/Export dialogs are Vista-style: `WM_SETTEXT`
  alone is ignored, so type the path with `WM_CHAR`, then click button id 1 (`_file_dialog` does this).
- **VirtualStore:** CM is not elevated, so its writes under `C:\Program Files\CircuitMaker 2000` land in
  `%LOCALAPPDATA%\VirtualStore\Program Files\CircuitMaker 2000`. Its INI, SEARCHDB.DAT and the "Save
  ASCII Library" output (always `UserLib.asc`, which exports User.lib only) live there. Keep working
  files elsewhere.
- Before taking control, check `CM.running_pids()`. If the user has CM open, ask before closing it.

## Menu ids (stable for this build; `python cmgui.py menus` lists them)
open 3, close 14, save 16, save_as 17, export waveform metafile 26, PCB netlist 28, SPICE netlist 29,
analog mode 109, digital mode 110, analyses setup 112, check pins 113, reset 116, step 117, run 118,
trace 119, split horizontal 74, arrow tool 85, Wave > Save As Text 134 (analog only; grayed in digital).

## Digital simulation — measured behaviour
- **Run** (`WM_COMMAND 118`) starts free-running. Sending 118 again does **not** stop it. The **F10 key**
  (real keystroke with CM in the foreground) toggles stop and keeps the history. **Reset** (116) stops
  *and clears*.
- **Step** (117) advances exactly **1 tick**, synchronously (`SendMessage`; 150 steps take ~0.1 s). The
  Step Size edit on the Digital tab can be changed with `WM_SETTEXT` (it reads back), but CM ignores it.
  So stepping N ticks = N Step messages (`step_ticks(n)`).
- After Reset: ripple counter outputs are X for 1 tick, then 0. The Pulser output is LOW for `low+1`
  ticks, then alternates; its k-th falling edge is at `t = 1 + k·(low+high)`.
- 74LS90 in CM ripples internally about 1 tick per stage. For example, from the 4th clock fall to Q2
  rising takes 3 ticks (Q0, then Q1, then Q2).
- Wire colors while simulating: red = 1, blue = 0, green = X/unknown.
- Logic Switches toggle on a **real** mouse click (`click_canvas(x, y)`, which uses schematic
  coordinates; canvas pixels = sheet coordinates at 100 % zoom, unscrolled). Posted mouse messages are
  ignored because CM reads the real cursor. Double-click opens a device's dialog (e.g. Edit Pulser).
  A click is never "swallowed": when a toggle seemed lost, the switch had started in the other state
  (its state is a[10], not the label). Confirm toggles with `logic_at(x, y)` on the switch's wire
  (colour after one Step; the colour does not change until the simulator steps).
- **Hex Key during simulation:** click it (outline turns red = armed), then type the digit 0-F on the
  keyboard. Its outputs change at the next step.
- Measured delays: Pulser edge -> 2-input OR output 1 tick; CLKG rising -> 7493 Q3 (via a 74LS04 on CP0,
  4 internal stages) 5 ticks. A gated clock whose stop signal comes from a 7493 needs high time > ~7.

## Reading results
1. **Waveform export** (File > Export > Waveform Metafile, id 26). The WMF draws each SCOPE probe's
   name (TextOut at x=13) and a polyline at 8 px per tick: baseline = 0, baseline−11 = 1, baseline−5 = X.
   It contains only the **last 180 ticks** (not the scroll history, and it ignores X Magnification).
   `digital_trace(total)` steps 150 ticks, exports, keeps the new 150 samples, and repeats, giving exact
   per-tick traces from t=0. `wavewmf.decode()` parses one file.
2. **Hex Display pixels.** Lit segments are pure red at fixed offsets from the display origin (x, y):
   a(0,−24) b(+7,−18) c(+7,−6) d(0,0) e(−7,−6) f(−7,−18) g(0,−12). `read_hex_displays()` requires two
   identical consecutive captures. It uses `screen_canvas()` (ImageGrab of the visible window):
   PrintWindow (`capture`) repeatedly returned half-drawn canvases while simulating (wires missing).
   Measured glyphs (calibration circuit, Hex Keys 0..F): 0-9 standard, A=abcefg, **B=cdefg (lowercase b)**,
   C=adef, D=bcdeg (lowercase d), E=adefg, F=aefg. 6 includes segment a.
4. **Wire colours.** `logic_at(x, y)` reads red/blue/green at a schematic point (needs CM visible).
5. **Netlist oracle.** Save As makes CM write its own per-pin net ids. `cm_verify.py nets` compares them
   with `ckt.build_nets` (ground symbols form one global net in both).

## Things that did not work (don't retry)
- Command-line open. Posted mouse messages for double-click. `WM_COMMAND 118` as stop. Step Size via
  messages. Wave > Save As Text in Digital mode (grayed). Reading Delphi TLabel text from message boxes
  (no HWND; `popups()` saves a PNG instead). `GetWindowText` on another process's edit returns a stale
  caption; use `WM_GETTEXT`.

## Help file
`Cirmaker.hlp` is compressed WinHelp. Its index confirms Pulser, Probe Tool states (H/L/pulse/unknown/Z),
Step button and Digital options (step size, tick increment), but the topic text needs a WinHelp
decompiler to read.
