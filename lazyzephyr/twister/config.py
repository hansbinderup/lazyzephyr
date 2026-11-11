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

Relative paths are resolved against the directory holding the file.
Command line options override values from the file.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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


_STR_LISTS = ("roots", "platforms", "watch", "extra_args")
_BOOLS = ("integration", "verbose", "auto_run")
_KNOWN = {*_STR_LISTS, *_BOOLS, "twister", "jobs"}


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
        raise ConfigError(f"{path}: unknown key(s): {', '.join(unknown)} "
                          f"(known: {', '.join(sorted(_KNOWN))})")

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
    if jobs is not None and (isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1):
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
    )
