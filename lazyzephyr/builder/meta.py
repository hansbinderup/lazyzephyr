"""What a workspace offers for building: apps, revisions, shields, snippets,
runners, and the debug probes plugged in."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ..dts.workspace import Board, Workspace, load_yaml

_SKIP = {".git", ".west", ".venv", "venv", "__pycache__", "node_modules", "build", "twister-out",
         "modules", "external", "bootloader"}


def _skip(name: str) -> bool:
    return name in _SKIP or name.startswith((".", "build-", "build_", "twister-out"))


# --------------------------------------------------------------------------
# Apps
# --------------------------------------------------------------------------


def find_apps(ws: Workspace, max_depth: int = 5) -> list[Path]:
    """Application folders (CMakeLists.txt + prj.conf): the workspace's own first,
    then Zephyr's samples."""
    own, samples = [], []
    for base, out, depth_limit in ((ws.topdir, own, max_depth),
                                   (ws.zephyr_base / "samples", samples, 6)):
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            here = Path(dirpath)
            if here == ws.zephyr_base:
                dirnames.clear()
                continue
            depth = len(here.relative_to(base).parts)
            if "CMakeLists.txt" in filenames and "prj.conf" in filenames:
                if not (here / "testcase.yaml").exists():
                    out.append(here)
            dirnames[:] = sorted(d for d in dirnames if not _skip(d)) \
                if depth < depth_limit else []
    return own + samples


# --------------------------------------------------------------------------
# Revisions
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Revisions:
    default: str | None
    names: tuple[str, ...]
    format: str | None = None


def board_revisions(board: Board) -> Revisions:
    data = load_yaml(board.directory / "board.yml") or {}
    entries = data.get("boards") or ([data["board"]] if data.get("board") else [])
    for entry in entries:
        if isinstance(entry, dict) and entry.get("name") == board.name:
            rev = entry.get("revision") or {}
            names = tuple(str(r.get("name")) for r in rev.get("revisions") or []
                          if isinstance(r, dict) and r.get("name") is not None)
            default = rev.get("default")
            return Revisions(str(default) if default is not None else None, names,
                             rev.get("format"))
    return Revisions(None, ())


# --------------------------------------------------------------------------
# Shields and snippets
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Shield:
    name: str
    directory: Path
    full_name: str | None = None
    vendor: str | None = None


def find_shields(ws: Workspace) -> list[Shield]:
    shields: dict[str, Shield] = {}
    for root in [ws.zephyr_base, *ws.board_roots]:
        base = root / "boards" / "shields"
        if not base.is_dir():
            continue
        for directory in sorted(p for p in base.iterdir() if p.is_dir()):
            meta = load_yaml(directory / "shield.yml") or {}
            entries = meta.get("shields") or ([meta["shield"]] if meta.get("shield") else [])
            if entries:
                for e in entries:
                    if isinstance(e, dict) and e.get("name"):
                        shields.setdefault(e["name"], Shield(e["name"], directory,
                                                             e.get("full_name"), e.get("vendor")))
            else:
                for overlay in directory.glob("*.overlay"):
                    shields.setdefault(overlay.stem, Shield(overlay.stem, directory))
    return sorted(shields.values(), key=lambda s: s.name)


@dataclass(frozen=True)
class Snippet:
    name: str
    path: Path


def find_snippets(ws: Workspace) -> list[Snippet]:
    found: dict[str, Snippet] = {}
    roots = [ws.zephyr_base, *(m.path for m in ws.modules),
             *(m.dts_root for m in ws.modules if m.dts_root)]
    for root in dict.fromkeys(roots):
        base = root / "snippets"
        if base.is_dir():
            for yml in base.rglob("snippet.yml"):
                name = (load_yaml(yml) or {}).get("name")
                if name:
                    found.setdefault(name, Snippet(name, yml.parent))
    return sorted(found.values(), key=lambda s: s.name)


# --------------------------------------------------------------------------
# Runners
# --------------------------------------------------------------------------

_INCLUDE_RUNNER = re.compile(r"include\(\s*\S*boards/common/([\w.-]+?)\.boards?\.cmake\s*\)")
_RUNNER_ARGS = re.compile(r"board_runner_args\(\s*([\w-]+)")
_SET_FLASHER = re.compile(r"board_set_flasher(?:_ifnset)?\(\s*([\w-]+)")


@dataclass(frozen=True)
class Runners:
    names: tuple[str, ...]
    flash_default: str | None
    exact: bool  # from a build's runners.yaml rather than board.cmake


def board_runners(board: Board, build_dir: Path | None = None) -> Runners:
    """Runners from the build's runners.yaml if built, else from board.cmake."""
    if build_dir:
        for yaml_path in (build_dir / "zephyr" / "runners.yaml",
                          *sorted(build_dir.glob("*/zephyr/runners.yaml"))):
            data = load_yaml(yaml_path)
            if isinstance(data, dict) and data.get("runners"):
                return Runners(tuple(data["runners"]), data.get("flash-runner"), True)
    text = ""
    cmake = board.directory / "board.cmake"
    try:
        text = cmake.read_text()
    except OSError:
        pass
    names: list[str] = []
    for m in _INCLUDE_RUNNER.finditer(text):
        name = m.group(1).split("-")[0] if m.group(1).startswith("openocd") else m.group(1)
        if name not in names:
            names.append(name)
    flasher = _SET_FLASHER.search(text)
    default = flasher.group(1) if flasher else (names[0] if names else None)
    return Runners(tuple(names), default, False)


# --------------------------------------------------------------------------
# Debug probes
# --------------------------------------------------------------------------

# USB vendor id -> probe family.
PROBE_VENDORS = {
    "1366": "J-Link",
    "0483": "ST-Link",
    "0d28": "CMSIS-DAP",
    "1fc9": "NXP probe",
    "2e8a": "Raspberry Pi probe",
    "303a": "Espressif",
    "1915": "Nordic",
    "c251": "Keil ULINK",
    "15ba": "Olimex",
    "0403": "FTDI",
}


@dataclass(frozen=True)
class Probe:
    serial: str
    kind: str
    product: str

    @property
    def dev_id(self) -> str:
        """The id west's runners expect (J-Link wants the number without zeros)."""
        if self.kind == "J-Link":
            return self.serial.lstrip("0") or self.serial
        return self.serial


def find_probes(sysfs: Path = Path("/sys/bus/usb/devices")) -> list[Probe]:
    probes = []
    try:
        devices = sorted(sysfs.iterdir())
    except OSError:
        return []
    for dev in devices:
        try:
            vendor = (dev / "idVendor").read_text().strip().lower()
            serial = (dev / "serial").read_text().strip()
        except OSError:
            continue
        kind = PROBE_VENDORS.get(vendor)
        if not kind or not serial:
            continue
        try:
            product = (dev / "product").read_text().strip()
        except OSError:
            product = ""
        if vendor == "0403" and "jtag" not in product.lower() and "debug" not in product.lower():
            continue  # plain FTDI serial cables
        probes.append(Probe(serial, kind, product))
    return probes
