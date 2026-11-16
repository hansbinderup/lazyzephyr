"""Stands in for python: `-m venv` and `-m pip install` lay out a venv like the real ones.

The venv's python and west are small scripts that run this file and fake_west.py.
Appends its arguments as a JSON line to $FAKE_LOG.
"""

import json
import os
import stat
import sys
from pathlib import Path

HERE = Path(__file__).parent
args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["python", *args]) + "\n")


def script(path: Path, target: Path, venv: Path) -> None:
    path.write_text(f'#!/bin/sh\nFAKE_VENV="{venv}" exec "{sys.executable}" "{target}" "$@"\n')
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


if args[:2] == ["-m", "venv"]:
    venv = Path(args[2])
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.4\n")
    script(venv / "bin" / "python", HERE / "fake_python.py", venv)
    print(f"created {venv}", flush=True)
elif args[:3] == ["-m", "pip", "install"]:
    venv = Path(os.environ["FAKE_VENV"])
    if "west" in args:
        script(venv / "bin" / "west", HERE / "fake_west.py", venv)
    print("Successfully installed " + " ".join(a for a in args[3:] if a[0] != "-"), flush=True)
else:
    sys.exit(f"fake python: unexpected {args}")
