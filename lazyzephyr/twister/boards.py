"""Board setups per platform, and which platforms need no board.

Each platform has its own setup (serial port, runner, probe, fixtures), so a
setup made for one board never carries over to another. Setups made in the app
are saved per workspace in the state dir (`hardware.json`), so nothing is
written into the repo. `[hardware.<platform>]` in `.lazyzephyr.toml` and the
command line override the saved setup of their platform.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import yaml

from .jobs import Hardware

SAVED_NAME = "hardware.json"

# Twister platform types that run without a board (`type:` in the board's twister yaml).
SIMULATED_TYPES = {"native", "qemu", "sim", "unit"}
_SIMULATED_PREFIXES = ("native_sim", "native_posix", "qemu_", "unit_testing")


def _saved_file(state_dir: Path) -> Path:
    return state_dir / SAVED_NAME


def hardware_from_dict(data: object) -> Hardware:
    """A Hardware from saved JSON. Raises ValueError on anything malformed."""
    if not isinstance(data, dict):
        raise ValueError("not a mapping")
    known = {"enabled", "serial", "baud", "runner", "dev_id", "fixtures"}
    if set(data) - known:
        raise ValueError(f"unknown keys: {sorted(set(data) - known)}")
    for key in ("serial", "runner", "dev_id"):
        if data.get(key) is not None and (not isinstance(data[key], str) or not data[key]):
            raise ValueError(f"{key} must be a non-empty string")
    baud = data.get("baud")
    if baud is not None and (isinstance(baud, bool) or not isinstance(baud, int) or baud < 1):
        raise ValueError("baud must be a positive integer")
    fixtures = data.get("fixtures", [])
    if not isinstance(fixtures, list) or not all(isinstance(f, str) and f for f in fixtures):
        raise ValueError("fixtures must be a list of non-empty strings")
    return Hardware(enabled=bool(data.get("enabled", False)), serial=data.get("serial"),
                    baud=baud, runner=data.get("runner"), dev_id=data.get("dev_id"),
                    fixtures=list(fixtures))


def load_saved(state_dir: Path) -> dict[str, Hardware]:
    """Saved setups by platform; malformed entries (or a broken file) are skipped."""
    try:
        data = json.loads(_saved_file(state_dir).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    boards = {}
    for platform, entry in data.items():
        try:
            boards[str(platform)] = hardware_from_dict(entry)
        except ValueError:
            continue
    return boards


def save(state_dir: Path, boards: dict[str, Hardware]) -> None:
    """Save the setups, leaving out empty ones."""
    data = {}
    for platform, hw in sorted(boards.items()):
        if hw.is_empty:
            continue
        entry = {k: v for k, v in asdict(hw).items() if v not in (None, [], False)}
        entry["enabled"] = hw.enabled
        data[platform] = entry
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = _saved_file(state_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(_saved_file(state_dir))


def simulated_by_name(platform: str) -> bool:
    """A guess from the name alone: native_sim, qemu_*, *bsim* and the like."""
    name = platform.split("/")[0]
    return name.startswith(_SIMULATED_PREFIXES) or "bsim" in name


def board_type(directory: Path, platform: str) -> str | None:
    """`type:` from the board's twister yaml for `platform`, if one is found.

    A bare board name matches the first of its targets.
    """
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict):
            continue
        identifier = str(data.get("identifier", ""))
        if identifier == platform or identifier.startswith(f"{platform}/"):
            board_kind = data.get("type")
            return str(board_kind) if board_kind else None
    return None
