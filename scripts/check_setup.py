#!/usr/bin/env python3
"""Check that this machine can run the circuitmaker-ckt tools.

    python check_setup.py

Exit code 0 when everything needed is present.  CircuitMaker is looked up in
CM2000_DIR (default C:\\Program Files\\CircuitMaker 2000).
"""
from __future__ import annotations

import importlib
import os
import platform
import sys
from pathlib import Path

CM_DIR = Path(os.environ.get("CM2000_DIR", r"C:\Program Files\CircuitMaker 2000"))
results: list[bool] = []


def check(name: str, ok: bool, fix: str = "", required: bool = True) -> None:
    tag = "ok" if ok else ("MISSING" if required else "warning")
    print(f"[{tag:7}] {name}" + ("" if ok or not fix else f"\n          -> {fix}"))
    if required:
        results.append(ok)


def has_module(name: str) -> bool:
    try:
        importlib.import_module(name)
        return True
    except Exception:
        return False


v = sys.version_info
check(f"Python {v.major}.{v.minor}.{v.micro} (tested with 3.14)", v >= (3, 9),
      "install Python 3.9 or newer from python.org")
check(f"Windows ({platform.system()} {platform.release()})", platform.system() == "Windows",
      "CircuitMaker 2000 and the GUI automation (cmgui.py, cm_verify.py) need Windows")
check("pywin32 (win32gui)", has_module("win32gui"), "pip install pywin32")
check("Pillow (PIL)", has_module("PIL"), "pip install pillow")
check(f"CircuitMaker 2000 in {CM_DIR}", (CM_DIR / "cirmaker.exe").is_file(),
      "install CircuitMaker 2000, or set CM2000_DIR to its folder")
lib_ok = (CM_DIR / "Device.lib").is_file()
check("Device.lib (component library)", lib_ok, f"expected at {CM_DIR / 'Device.lib'}")
if lib_ok:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        import devlib
        pins = [p.name for p in devlib.symbol("74LS90").pins]
        check(f"devlib reads Device.lib (74LS90: {' '.join(pins)})", bool(pins))
    except Exception as e:  # noqa: BLE001
        check("devlib reads Device.lib", False, f"{type(e).__name__}: {e}")

print("\nready" if all(results) else "\nnot ready: fix the MISSING items above")
sys.exit(0 if all(results) else 1)
