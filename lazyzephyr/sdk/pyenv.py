"""The workspace's Python environment: a .venv with west and Zephyr's requirements.

Zephyr's getting-started guide puts a virtualenv in the west workspace and installs
west and Zephyr's Python requirements into it. lazyzephyr sets that up the same way
and then runs west, builds and twister from it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

MARKER = "lazyzephyr.json"  # in the venv: the requirements it was set up for
MIN_PYTHON = (3, 12)


def venv_path(topdir: Path) -> Path:
    return Path(topdir) / ".venv"


def venv_bin(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin")


def venv_west(topdir: Path | None) -> Path | None:
    """The workspace venv's west, if it has one."""
    if topdir is None:
        return None
    west = venv_bin(venv_path(topdir)) / "west"
    return west if west.exists() else None


def requirements_hash(zephyr_base: Path | None) -> str | None:
    """A fingerprint of Zephyr's requirement files, to notice when they change."""
    if zephyr_base is None:
        return None
    files = sorted((Path(zephyr_base) / "scripts").glob("requirements*.txt"))
    if not files:
        return None
    digest = hashlib.sha256()
    for f in files:
        try:
            digest.update(f.name.encode() + b"\0" + f.read_bytes() + b"\0")
        except OSError:
            return None
    return digest.hexdigest()[:16]


def _python_version(venv: Path) -> tuple[int, ...] | None:
    try:
        text = (venv / "pyvenv.cfg").read_text()
    except OSError:
        return None
    m = re.search(r"^version(?:_info)?\s*=\s*(\d+)\.(\d+)", text, re.MULTILINE)
    return (int(m.group(1)), int(m.group(2))) if m else None


@dataclass
class PyEnv:
    path: Path
    exists: bool
    python: tuple[int, ...] | None  # major, minor
    has_west: bool
    ours: bool  # set up by lazyzephyr (has our marker)
    up_to_date: bool  # set up for Zephyr's current requirement files

    @property
    def python_ok(self) -> bool:
        return self.python is None or self.python >= MIN_PYTHON

    @property
    def ready(self) -> bool:
        return self.exists and self.has_west and self.up_to_date and self.python_ok


def inspect(topdir: Path, zephyr_base: Path | None) -> PyEnv:
    venv = venv_path(topdir)
    exists = (venv / "pyvenv.cfg").is_file()
    try:
        marker = json.loads((venv / MARKER).read_text())
    except (OSError, ValueError):
        marker = None
    ours = isinstance(marker, dict)
    wanted = requirements_hash(zephyr_base)
    return PyEnv(
        path=venv,
        exists=exists,
        python=_python_version(venv) if exists else None,
        has_west=venv_west(topdir) is not None,
        ours=ours,
        up_to_date=ours and marker.get("requirements") == wanted,
    )


def mark_set_up(topdir: Path, zephyr_base: Path | None) -> None:
    venv = venv_path(topdir)
    (venv / MARKER).write_text(
        json.dumps({"requirements": requirements_hash(zephyr_base)}, indent=2) + "\n"
    )


def base_python() -> str:
    """The Python to create the venv with: the one lazyzephyr runs on (3.12 or later)."""
    return getattr(sys, "_base_executable", None) or sys.executable


def setup_commands(topdir: Path, zephyr_base: Path, python: str | None = None) -> list[list[str]]:
    """Create (or reuse) the venv, install west, then Zephyr's (and modules') requirements."""
    venv = venv_path(topdir)
    vpython = str(venv_bin(venv) / "python")
    commands = []
    if not (venv / "pyvenv.cfg").is_file():
        commands.append([python or base_python(), "-m", "venv", str(venv)])
    commands.append([vpython, "-m", "pip", "install", "--upgrade", "pip", "west"])
    if (Path(zephyr_base) / "scripts" / "west_commands" / "packages.py").is_file():
        commands.append([str(venv_bin(venv) / "west"), "packages", "pip", "--install"])
    else:  # Zephyr before `west packages`: its own requirements only
        requirements = Path(zephyr_base) / "scripts" / "requirements.txt"
        commands.append([vpython, "-m", "pip", "install", "-r", str(requirements)])
    return commands


def venv_env(env: dict[str, str], topdir: Path | None) -> dict[str, str]:
    """`env` with the workspace venv first on PATH, unless another venv is active."""
    if topdir is None or env.get("VIRTUAL_ENV"):
        return env
    if venv_west(topdir) is None:
        return env
    venv = venv_path(topdir)
    env = dict(env)
    env["VIRTUAL_ENV"] = str(venv)
    env["PATH"] = os.pathsep.join(p for p in (str(venv_bin(venv)), env.get("PATH", "")) if p)
    env.pop("PYTHONHOME", None)
    return env
