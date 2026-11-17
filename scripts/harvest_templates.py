#!/usr/bin/env python3
"""Add component records from CircuitMaker-saved .CKT files to assets/templates.json.

Use when build.py says "no app-saved template for symbol X" or when a
synthesized part misbehaves: place the device once in CircuitMaker, save the
file (outside Program Files), then

    python harvest_templates.py saved.CKT [more.CKT ...] [--replace]

Only files written by CircuitMaker itself should be harvested (a template is
trusted because CM wrote it).  Existing entries are kept unless --replace.
"""
import json
import sys
from pathlib import Path

import ckt

ASSETS = Path(__file__).resolve().parent.parent / "assets" / "templates.json"


def main(argv):
    replace = "--replace" in argv
    files = [a for a in argv if not a.startswith("--")]
    data = json.loads(ASSETS.read_text("utf-8"))
    added = []
    for f in files:
        c = ckt.Circuit.load(f)
        if c.version != "5.6":
            print(f"warning: {f} is version {c.version}; prefer files saved by this CM build (5.6)")
        for comp in c.components:
            if comp.name not in data["records"] or replace:
                data["records"][comp.name] = comp.raw
                data["source"][comp.name] = Path(f).name + " v" + c.version
                added.append(comp.name)
    ASSETS.write_text(json.dumps(data, indent=0), "utf-8")
    print("added:", ", ".join(sorted(set(added))) or "(nothing new)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
