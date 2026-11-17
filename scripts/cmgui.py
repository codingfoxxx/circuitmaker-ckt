#!/usr/bin/env python3
"""Drive CircuitMaker 2000 (cirmaker.exe, a Delphi app) without touching the mouse.

Everything goes through window messages: menu commands are posted as
WM_COMMAND with the menu item id, common dialogs are filled with WM_SETTEXT,
and screenshots use PrintWindow (works even if the window is covered).

Typical use (also available as CLI, see ``--help``)::

    from cmgui import CM
    cm = CM.start()                       # or CM.attach()
    errs = cm.open(r"C:\\work\\x.CKT")     # returns texts of any popups
    cm.save_as(r"C:\\work\\x_resaved.CKT") # CM recomputes nets on save
    cm.screenshot(r"C:\\work\\x.png")
    cm.quit()

Notes / pitfalls (verified):
* The command line does NOT open files; use ``open()``.
* Never save into ``C:\\Program Files\\CircuitMaker 2000``: Windows silently
  redirects writes to ``%LOCALAPPDATA%\\VirtualStore\\Program Files\\...``.
* Menu ids are stable for this build (see MENU); ``menu_ids()`` re-reads them.
* Popups: Delphi message boxes (TMessageForm) draw their text with TLabel,
  which has no window handle; ``popups()`` therefore also saves a PNG of each
  popup so the text can be read from the image.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from pathlib import Path

import win32con
import win32gui
import win32process
import win32ui

EXE = Path(os.environ.get("CM2000_DIR", r"C:\Program Files\CircuitMaker 2000")) / "cirmaker.exe"
user32 = ctypes.windll.user32

MENU = {
    "new": 2, "open": 3, "close": 14, "save": 16, "save_as": 17, "revert": 18,
    "export_pcb_netlist": 28, "export_spice_netlist": 29, "bom": 31, "exit": 39,
    "select_all": 53, "zoom_fit": 79, "refresh": 81,
    "analog_mode": 109, "digital_mode": 110, "analyses_setup": 112,
    "check_pins": 113, "check_wires": 114,
    "reset": 116, "step": 117, "run": 118, "trace": 119,
    "wave_save_text": 134, "circuit_info": 140,
}
IGNORED_CLASSES = {"TApplication", "MSCTFIME UI", "IME", "tooltips_class32", "ComboLBox",
                   "TPUtilWindow"}


def _enum_children(h):
    out = []

    def cb(c, _):
        out.append(c)
        return True
    try:
        win32gui.EnumChildWindows(h, cb, None)
    except Exception:
        pass
    return out


def capture(hwnd: int, out: str | os.PathLike, crop=None):
    """PrintWindow screenshot of ``hwnd`` saved as PNG; returns PIL image."""
    from PIL import Image
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    hdc = win32gui.GetWindowDC(hwnd)
    mdc = win32ui.CreateDCFromHandle(hdc)
    sdc = mdc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mdc, w, h)
    sdc.SelectObject(bmp)
    user32.PrintWindow(hwnd, sdc.GetSafeHdc(), 2)
    info = bmp.GetInfo()
    im = Image.frombuffer("RGB", (info["bmWidth"], info["bmHeight"]), bmp.GetBitmapBits(True),
                          "raw", "BGRX", 0, 1)
    win32gui.DeleteObject(bmp.GetHandle())
    sdc.DeleteDC()
    mdc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hdc)
    if crop:
        im = im.crop(crop)
    im.save(out)
    return im


class CM:
    def __init__(self, pid: int, proc: subprocess.Popen | None = None):
        self.pid = pid
        self.proc = proc
        self.shots_dir = Path(os.environ.get("TEMP", ".")) / "cmgui_popups"
        self.shots_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ process
    @staticmethod
    def running_pids() -> list[int]:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq cirmaker.exe", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, encoding="mbcs", errors="replace").stdout or ""
        pids = []
        for ln in out.splitlines():
            parts = [p.strip('"') for p in ln.split('","')]
            if len(parts) > 1 and parts[0].lower().strip('"') == "cirmaker.exe":
                pids.append(int(parts[1]))
        return pids

    @classmethod
    def attach(cls) -> "CM":
        pids = cls.running_pids()
        if not pids:
            raise RuntimeError("CircuitMaker is not running")
        return cls(pids[0])

    @classmethod
    def start(cls, timeout: float = 30) -> "CM":
        """Start a fresh instance (refuses if one is already running)."""
        if cls.running_pids():
            raise RuntimeError("CircuitMaker already running; use CM.attach() or close it first")
        proc = subprocess.Popen([str(EXE)], cwd=str(EXE.parent))
        cm = cls(proc.pid, proc)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cm.main_window(raise_=False):
                time.sleep(1.0)
                cm.dismiss_startup()
                return cm
            time.sleep(0.3)
        raise RuntimeError("CircuitMaker main window did not appear")

    def dismiss_startup(self):
        for h, title, cls_ in self.windows():
            if cls_ not in ("TMainForm",):
                win32gui.PostMessage(h, win32con.WM_CLOSE, 0, 0)

    # ------------------------------------------------------------ windows
    def windows(self):
        acc = []

        def cb(h, _):
            _, p = win32process.GetWindowThreadProcessId(h)
            if p == self.pid and win32gui.IsWindowVisible(h):
                cls_ = win32gui.GetClassName(h)
                if cls_ not in IGNORED_CLASSES:
                    acc.append((h, win32gui.GetWindowText(h), cls_))
            return True
        win32gui.EnumWindows(cb, None)
        return acc

    def _all_windows(self):
        acc = []

        def cb(h, _):
            _, p = win32process.GetWindowThreadProcessId(h)
            if p == self.pid:
                acc.append((h, win32gui.GetClassName(h), win32gui.IsWindowVisible(h)))
            return True
        win32gui.EnumWindows(cb, None)
        return acc

    def main_window(self, raise_: bool = True):
        """The TMainForm.  If the app is minimized (Delphi hides the form and
        keeps only the TApplication window) it is restored first."""
        allw = self._all_windows()
        mains = [h for h, c, v in allw if c == "TMainForm"]
        if mains and not win32gui.IsWindowVisible(mains[0]):
            for h, c, v in allw:
                if c == "TApplication":
                    win32gui.PostMessage(h, win32con.WM_SYSCOMMAND, win32con.SC_RESTORE, 0)
            for _ in range(20):
                time.sleep(0.15)
                if win32gui.IsWindowVisible(mains[0]):
                    break
        if mains:
            return mains[0]
        if raise_:
            raise RuntimeError("main window not found")
        return None

    @property
    def title(self) -> str:
        return win32gui.GetWindowText(self.main_window())

    def popups(self, snap: bool = True) -> list[dict]:
        """Visible windows other than the main form: dialogs, message boxes."""
        out = []
        for h, title, cls_ in self.windows():
            if cls_ == "TMainForm":
                continue
            kids = _enum_children(h)
            texts = [win32gui.GetWindowText(c) for c in kids if win32gui.GetWindowText(c)]
            buttons = [(c, win32gui.GetWindowText(c)) for c in kids
                       if win32gui.GetClassName(c) in ("TButton", "Button", "TBitBtn")]
            item = {"hwnd": h, "title": title, "class": cls_, "texts": texts, "buttons": buttons}
            if snap:
                p = self.shots_dir / f"popup_{h}.png"
                try:
                    capture(h, p)
                    item["png"] = str(p)
                except Exception:
                    pass
            out.append(item)
        return out

    def wait_popup(self, timeout=5.0, cls=None, title_has=None):
        t0 = time.time()
        while time.time() - t0 < timeout:
            for p in self.popups(snap=False):
                if cls and p["class"] != cls:
                    continue
                if title_has and title_has.lower() not in p["title"].lower():
                    continue
                return p
            time.sleep(0.15)
        return None

    def click_button(self, popup: dict, *labels: str) -> bool:
        want = [l.replace("&", "").lower() for l in labels]
        for h, txt in popup["buttons"]:
            if txt.replace("&", "").lower() in want:
                win32gui.PostMessage(h, win32con.BM_CLICK, 0, 0)
                return True
        return False

    # ------------------------------------------------------------ commands
    def menu_ids(self) -> dict[str, int]:
        res = {}

        def walk(hm, prefix=""):
            for i in range(user32.GetMenuItemCount(hm)):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetMenuStringW(hm, i, buf, 256, 0x400)
                name = buf.value.replace("&", "").split("\t")[0]
                sub = user32.GetSubMenu(hm, i)
                if sub:
                    walk(sub, prefix + name + "/")
                elif name:
                    res[prefix + name] = user32.GetMenuItemID(hm, i)
        walk(user32.GetMenu(self.main_window()))
        return res

    def menu_state(self, cmd: str | int) -> dict:
        cid = MENU.get(cmd, cmd) if isinstance(cmd, str) else cmd
        st = user32.GetMenuState(user32.GetMenu(self.main_window()), cid, 0)
        return {"grayed": bool(st & 3), "checked": bool(st & 8)}

    def cmd(self, name: str | int, wait: float = 0.3):
        cid = MENU[name] if isinstance(name, str) else name
        win32gui.PostMessage(self.main_window(), win32con.WM_COMMAND, cid, 0)
        time.sleep(wait)

    def _file_dialog(self, path: str, timeout=8.0):
        dlg = self.wait_popup(timeout=timeout, cls="#32770")
        if not dlg:
            raise RuntimeError("file dialog did not open")
        h = dlg["hwnd"]
        edit = 0
        try:                       # old-style (Win3.1 look) dialog
            edit = win32gui.GetDlgItem(h, 1152)
        except Exception:
            edit = 0
        if not edit:               # Vista-style dialog: Edit inside ComboBoxEx (id 1001)
            for c in _enum_children(h):
                if win32gui.GetClassName(c) == "Edit" and win32gui.GetDlgCtrlID(c) in (1001, 1148):
                    edit = c
                    break
        if not edit:
            raise RuntimeError("filename box not found in dialog")
        # WM_SETTEXT alone is ignored by Vista-style dialogs; type it instead.
        win32gui.SendMessage(edit, win32con.WM_SETTEXT, 0, "")
        for ch in str(path):
            win32gui.SendMessage(edit, win32con.WM_CHAR, ord(ch), 0)
        time.sleep(0.2)
        ok = 0
        try:
            ok = win32gui.GetDlgItem(h, 1)
        except Exception:
            ok = 0
        if ok:
            win32gui.PostMessage(ok, win32con.BM_CLICK, 0, 0)
        else:
            win32gui.PostMessage(h, win32con.WM_COMMAND, 1, 0)
        return h

    def _settle(self, seconds=1.0):
        time.sleep(seconds)
        return [p for p in self.popups()]

    def open(self, path: str | os.PathLike, timeout=15.0) -> list[dict]:
        """Open a .CKT; returns popups that appeared (errors/warnings)."""
        path = str(Path(path).resolve())
        self.close_file()
        self.cmd("open")
        self._file_dialog(path)
        base = Path(path).name.lower()
        t0 = time.time()
        while time.time() - t0 < timeout:
            pops = [p for p in self.popups(snap=False) if p["class"] != "#32770"]
            if pops:
                time.sleep(0.5)
                return self.popups()
            if base in self.title.lower():
                time.sleep(0.8)
                return self.popups()
            time.sleep(0.2)
        return self.popups()

    def save(self):
        self.cmd("save", wait=1.0)
        return self.popups()

    def save_as(self, path: str | os.PathLike, timeout=10.0) -> list[dict]:
        path = str(Path(path).resolve())
        if Path(path).exists():
            Path(path).unlink()
        self.cmd("save_as")
        self._file_dialog(path)
        t0 = time.time()
        while time.time() - t0 < timeout:
            if Path(path).exists():
                time.sleep(0.5)
                return self.popups()
            pops = [p for p in self.popups(snap=False)]
            if pops:
                time.sleep(0.3)
                return self.popups()
            time.sleep(0.2)
        raise RuntimeError(f"save_as did not produce {path}")

    def close_file(self, discard: bool = True):
        """Close the current schematic, answering 'No' to save prompts."""
        if "UNTITLED" in self.title.upper() and "*" not in self.title:
            return
        self.cmd("new", wait=0.6)
        p = self.wait_popup(timeout=1.5)
        if p:
            self.click_button(p, "No", "Não", "&No") if discard else self.click_button(p, "Yes", "Sim")
            time.sleep(0.6)

    def screenshot(self, out: str | os.PathLike, crop=None):
        return capture(self.main_window(), out, crop)

    # ------------------------------------------------------------ simulation
    def digital_mode(self):
        if not self.menu_state("digital_mode")["checked"]:
            self.cmd("digital_mode", wait=0.6)

    def analog_mode(self):
        if not self.menu_state("analog_mode")["checked"]:
            self.cmd("analog_mode", wait=0.6)

    def _digital_edits(self):
        """The three TEdits of the left 'Digital' tab: (step_size, x_magnification, speed),
        identified by vertical order inside the 'Options' group box."""
        m = self.main_window()
        box = None
        for c in _enum_children(m):
            if win32gui.GetClassName(c) == "TGroupBox" and win32gui.GetWindowText(c) == "Options":
                box = c
                break
        if not box:
            raise RuntimeError("Digital options panel not found (is Digital mode on?)")
        edits = [c for c in _enum_children(box) if win32gui.GetClassName(c) == "TEdit"]
        edits.sort(key=lambda h: win32gui.GetWindowRect(h)[1])
        return edits

    def set_digital_options(self, step_ticks: int | None = None, xmag: int | None = None):
        step, xm, _speed = self._digital_edits()
        if step_ticks is not None:
            win32gui.SendMessage(step, win32con.WM_SETTEXT, 0, str(step_ticks))
        if xmag is not None:
            win32gui.SendMessage(xm, win32con.WM_SETTEXT, 0, str(xmag))
        time.sleep(0.2)
        return [win32gui.GetWindowText(h) for h in (step, xm, _speed)]

    def export_waveforms(self, path: str | os.PathLike) -> str:
        """File > Export > Waveform Metafile (digital waveform window -> WMF)."""
        path = str(Path(path).resolve())
        if Path(path).exists():
            Path(path).unlink()
        self.cmd(26, wait=0.8)
        self._file_dialog(path)
        t0 = time.time()
        while time.time() - t0 < 10 and not Path(path).exists():
            time.sleep(0.2)
        time.sleep(0.5)
        return path

    def show_waveforms(self):
        m = self.main_window()
        for c in _enum_children(m):
            if win32gui.GetClassName(c) == "DigitalWaveWindow":
                if not win32gui.IsWindowVisible(c):
                    self.cmd(74, wait=0.8)          # View > Split Horizontal
                return c
        self.cmd(74, wait=0.8)

    def step_ticks(self, n: int):
        """Advance the digital simulation n ticks (one Step = 1 tick; the Step Size
        edit cannot be changed by messages).  Synchronous."""
        m = self.main_window()
        for _ in range(n):
            win32gui.SendMessage(m, win32con.WM_COMMAND, MENU["step"], 0)

    def click_canvas(self, x: int, y: int, double: bool = False):
        """Real mouse click at schematic coords (100 % zoom, unscrolled view).
        Used to toggle Logic Switches during simulation.  Restores the cursor."""
        import win32api
        m = self.main_window()
        win32api.keybd_event(0x12, 0, 0, 0)
        win32api.keybd_event(0x12, 0, 2, 0)
        try:
            win32gui.SetForegroundWindow(m)
        except Exception:
            pass
        time.sleep(0.2)
        canvas = [c for c in _enum_children(m) if win32gui.GetClassName(c) == "CirmakerEdit"][0]
        sx, sy = win32gui.ClientToScreen(canvas, (x, y))
        old = win32api.GetCursorPos()
        win32api.SetCursorPos((sx, sy))
        time.sleep(0.1)
        for _ in range(2 if double else 1):
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.05)
        time.sleep(0.3)
        win32api.SetCursorPos(old)

    def screen_canvas(self):
        """Canvas as shown on screen (ImageGrab).  PrintWindow (capture) can return a
        half-repainted canvas while the simulator is redrawing wires."""
        from PIL import ImageGrab
        m = self.main_window()
        canvas = [c for c in _enum_children(m) if win32gui.GetClassName(c) == "CirmakerEdit"][0]
        time.sleep(0.6)
        return ImageGrab.grab(bbox=win32gui.GetWindowRect(canvas), all_screens=True).convert("RGB")

    def logic_at(self, x: int, y: int, img=None) -> str:
        """Logic value of the wire passing through schematic point (x, y) during
        simulation, from its colour: red = 1, blue = 0, green = X ('?' otherwise)."""
        im = img or self.screen_canvas()
        for dx, dy in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):
            r, g, b = im.getpixel((x + dx, y + dy))
            if r > 200 and g < 60 and b < 60:
                return "1"
            if b > 200 and r < 60 and g < 60:
                return "0"
            if g > 200 and r < 60 and b < 60:
                return "X"
        return "?"

    def digital_trace(self, total: int, chunk: int = 150, actions: dict | None = None,
                      tmpdir: str | None = None) -> dict[str, list]:
        """Reset and simulate ``total`` ticks in Digital mode; return every SCOPE
        probe's value at each tick (index 0 = first tick after reset), decoded
        from Waveform-Metafile exports taken every ``chunk`` ticks (<=170, the
        export only holds the last 180 ticks).  ``actions`` = {tick: callable}
        runs callable (e.g. a switch click) when that tick is reached."""
        import wavewmf
        chunk = min(chunk, 170)
        tmpdir = Path(tmpdir or os.environ.get("TEMP", "."))
        self.digital_mode()
        self.show_waveforms()
        self.cmd("reset", wait=0.5)
        stops = sorted(set(list(range(chunk, total, chunk)) + [total] + list((actions or {}).keys())))
        out: dict[str, list] = {}
        t = 0
        k = 0
        for stop in stops:
            if stop > total:
                break
            n = stop - t
            if n > 0:
                self.step_ticks(n)
                k += 1
                w = wavewmf.decode(self.export_waveforms(tmpdir / f"cm_trace_{k}.wmf"))
                for name, s in w.items():
                    out.setdefault(name, []).extend(s[-n:])
                t = stop
            if actions and stop in actions:
                actions[stop]()
        return out

    # ---------------------------------------------------------- display reading
    SEG_PTS = {"a": (0, -24), "b": (7, -18), "c": (7, -6), "d": (0, 0),
               "e": (-7, -6), "f": (-7, -18), "g": (0, -12)}
    # glyphs measured in CM (calibration circuit: Hex Keys 0..F -> Hex Displays); B is a lowercase "b"
    HEX = {"abcdef": "0", "bc": "1", "abdeg": "2", "abcdg": "3", "bcfg": "4", "acdfg": "5",
           "acdefg": "6", "abc": "7", "abcdefg": "8", "abcdfg": "9",
           "abcefg": "A", "cdefg": "B", "adef": "C", "bcdeg": "D", "adefg": "E", "aefg": "F", "": " "}

    def canvas_image(self):
        import tempfile
        m = self.main_window()
        canvas = [c for c in _enum_children(m) if win32gui.GetClassName(c) == "CirmakerEdit"][0]
        return capture(canvas, Path(tempfile.gettempdir()) / "cm_canvas.png").convert("RGB")

    def read_hex_displays(self, positions: dict[str, tuple[int, int]], img=None) -> dict[str, str]:
        """Read CircuitMaker 'Hex Display' digits from the canvas (100 % zoom,
        unscrolled).  positions = {name: (x, y) of the display component}.
        Lit segments are pure red; '?' = segment pattern not recognised."""
        def once(im):
            out = {}
            for name, (x, y) in positions.items():
                lit = ""
                for seg, (dx, dy) in self.SEG_PTS.items():
                    r, g, b = im.getpixel((x + dx, y + dy))
                    if r > 160 and g < 90 and b < 90:
                        lit += seg
                out[name] = self.HEX.get(lit, "?")
            return out
        if img is not None:
            return once(img)
        # the canvas may be mid-repaint: require two identical consecutive reads
        # (screen grab: PrintWindow returned half-drawn canvases during simulation)
        self.canvas_image = self.screen_canvas
        prev = once(self.canvas_image())
        for _ in range(8):
            time.sleep(0.15)
            cur = once(self.canvas_image())
            if cur == prev and " " not in "".join(cur.values()) and "?" not in "".join(cur.values()):
                return cur
            if cur == prev:
                time.sleep(0.25)
            prev = cur
        return prev

    def quit(self):
        self.close_file()
        self.cmd("exit", wait=1.0)
        p = self.wait_popup(timeout=1.0)
        if p:
            self.click_button(p, "No", "Não")
        if self.proc:
            try:
                self.proc.wait(timeout=5)
            except Exception:
                pass


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="CircuitMaker 2000 automation")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("resave", help="open each .CKT in CM and Save As <name>_cm.CKT (CM recomputes nets)")
    s.add_argument("files", nargs="+")
    s.add_argument("--shot", action="store_true", help="also save a screenshot next to the file")
    s = sub.add_parser("shot", help="open a .CKT and screenshot it")
    s.add_argument("file")
    s.add_argument("png")
    s = sub.add_parser("menus", help="print menu ids")
    a = ap.parse_args(argv)
    cm = CM.attach() if CM.running_pids() else CM.start()
    if a.cmd == "menus":
        for k, v in cm.menu_ids().items():
            print(v, k)
        return 0
    if a.cmd == "shot":
        pops = cm.open(a.file)
        cm.screenshot(a.png)
        print("popups:", [(p["title"], p["texts"], p.get("png")) for p in pops])
        return 0
    if a.cmd == "resave":
        for f in a.files:
            f = Path(f).resolve()
            pops = cm.open(f)
            if pops:
                print(f"{f.name}: POPUPS on open:", [(p["title"], p["texts"], p.get("png")) for p in pops])
                for p in pops:
                    cm.click_button(p, "OK", "Ok", "Yes", "Sim")
                time.sleep(0.5)
            out = f.with_name(f.stem + "_cm" + f.suffix)
            pops2 = cm.save_as(out)
            if a.shot:
                cm.screenshot(f.with_suffix(".png"))
            print(f"{f.name} -> {out.name}", "popups:" if pops2 else "", [(p["title"], p["texts"]) for p in pops2])
        return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
