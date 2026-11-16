"""Per-workspace defaults from a `.lazyzephyr.toml` file.

Example::

    roots = ["tests"]
    platforms = ["native_sim/native/64"]
    integration = true          # pass -G
    verbose = true              # pass -v (live per-scenario status)
    auto_run = false
    watch = ["lib", "subsys"]   # extra paths that trigger auto-run
    twister = "west twister"
    jobs = 8                    # twister -j
    extra_args = ["--coverage"]
    case_results = true         # print case results in Output when a job ends

    [hardware.frdm_k64f]        # this platform's board (twister --device-testing)
    enabled = true              # default: true if serial is set
    serial = "/dev/ttyACM0"     # console UART
    baud = 115200
    runner = "jlink"            # west flash runner; default: the board's
    dev_id = "000683"           # debug probe serial
    fixtures = ["gpio_loopback"]

    [hardware."nrf52840dk/nrf52840"]   # quote names that contain "/"
    serial = "/dev/ttyACM1"

A platform's board setup only applies when it is the one chosen platform.

Relative paths are resolved against the directory holding the file.
Command line options override values from the file. Settings changed in the app
(platforms, case_results, integration, auto_run) are remembered and override the
file too, see session.py.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .jobs import Hardware

CONFIG_NAME = ".lazyzephyr.toml"


class ConfigError(ValueError):
    pass


@dataclass
class FileConfig:
    path: Path | None = None
    roots: list[Path] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    integration: bool | None = None
    verbose: bool | None = None
    auto_run: bool | None = None
    watch: list[Path] = field(default_factory=list)
    twister: str | None = None
    jobs: int | None = None
    extra_args: list[str] = field(default_factory=list)
    case_results: bool | None = None
    boards: dict[str, Hardware] = field(default_factory=dict)  # [hardware.<platform>]


_STR_LISTS = ("roots", "platforms", "watch", "extra_args")
_BOOLS = ("integration", "verbose", "auto_run", "case_results")
_KNOWN = {*_STR_LISTS, *_BOOLS, "twister", "jobs", "hardware"}
_HARDWARE_STRS = ("serial", "runner", "dev_id")
_HARDWARE_KNOWN = {*_HARDWARE_STRS, "enabled", "baud", "fixtures"}


def _positive_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def load_boards(path: Path, data) -> dict[str, Hardware]:
    """`[hardware.<platform>]` tables: one board setup per platform."""
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: 'hardware' must be a table ([hardware.<platform>])")
    flat = sorted(k for k, v in data.items() if not isinstance(v, dict))
    if flat:
        raise ConfigError(
            f"{path}: put board settings under [hardware.<platform>], "
            f"e.g. [hardware.frdm_k64f] (found: {', '.join(flat)})"
        )
    return {platform: load_hardware(path, platform, table) for platform, table in data.items()}


def load_hardware(path: Path, platform: str, data: dict) -> Hardware:
    where = f"hardware.{platform}" if "/" not in platform else f'hardware."{platform}"'
    unknown = sorted(set(data) - _HARDWARE_KNOWN)
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s) in [{where}]: {', '.join(unknown)} "
            f"(known: {', '.join(sorted(_HARDWARE_KNOWN))})"
        )
    if "enabled" in data and not isinstance(data["enabled"], bool):
        raise ConfigError(f"{path}: '{where}.enabled' must be true or false")
    for key in _HARDWARE_STRS:
        if key in data and (not isinstance(data[key], str) or not data[key].strip()):
            raise ConfigError(f"{path}: '{where}.{key}' must be a non-empty string")
    if "baud" in data and not _positive_int(data["baud"]):
        raise ConfigError(f"{path}: '{where}.baud' must be a positive integer")
    fixtures = data.get("fixtures", [])
    if not isinstance(fixtures, list) or not all(isinstance(f, str) and f for f in fixtures):
        raise ConfigError(f"{path}: '{where}.fixtures' must be a list of non-empty strings")
    return Hardware(
        # a serial port alone means "run on the board"
        enabled=data.get("enabled", "serial" in data),
        serial=data.get("serial"),
        baud=data.get("baud"),
        runner=data.get("runner"),
        dev_id=data.get("dev_id"),
        fixtures=fixtures,
    )


def find_config(start: Path, stop: Path | None) -> Path | None:
    """Look for the config file in `start` and its parents, up to `stop` (inclusive)."""
    start = start.resolve()
    stop = stop.resolve() if stop else None
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
        if directory == stop:
            break
    return None


def load_config(path: Path) -> FileConfig:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc

    unknown = sorted(set(data) - _KNOWN)
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s): {', '.join(unknown)} (known: {', '.join(sorted(_KNOWN))})"
        )

    def str_list(key: str) -> list[str]:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
            raise ConfigError(f"{path}: '{key}' must be a list of non-empty strings")
        return value

    for key in _BOOLS:
        if key in data and not isinstance(data[key], bool):
            raise ConfigError(f"{path}: '{key}' must be true or false")
    twister = data.get("twister")
    if twister is not None and (not isinstance(twister, str) or not twister.strip()):
        raise ConfigError(f"{path}: 'twister' must be a non-empty string")
    jobs = data.get("jobs")
    if jobs is not None and not _positive_int(jobs):
        raise ConfigError(f"{path}: 'jobs' must be a positive integer")

    base = path.parent.resolve()
    return FileConfig(
        path=path.resolve(),
        roots=[(base / r).resolve() for r in str_list("roots")],
        platforms=str_list("platforms"),
        integration=data.get("integration"),
        verbose=data.get("verbose"),
        auto_run=data.get("auto_run"),
        watch=[(base / w).resolve() for w in str_list("watch")],
        twister=twister,
        jobs=jobs,
        extra_args=str_list("extra_args"),
        case_results=data.get("case_results"),
        boards=load_boards(path, data["hardware"]) if "hardware" in data else {},
    )
