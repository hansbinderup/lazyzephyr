"""lazyzephyr's on-disk cache: twister build output and the results history.

Layout: <cache root>/<workspace slug>/{run/, results.json}. Deletion only ever
touches entries with names lazyzephyr itself creates, so pointing
--state-dir somewhere unusual can't make it remove unrelated files.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

RESULTS_FILES = ("results.json", "results.tmp")
# run/ plus leftovers from older versions: run.N (twister rotation), run-N, run-N.M
_BUILD_DIR_RE = re.compile(r"^run(?:-\d+)?(?:\.\d+)?$")


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "lazyzephyr"


def state_dir_for(anchor: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", str(anchor.resolve())).strip("_")
    return cache_root() / slug


def dir_size(path: Path) -> int:
    """Total size in bytes, without following symlinks."""
    if path.is_symlink() or path.is_file():
        try:
            return path.lstat().st_size
        except OSError:
            return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def build_dirs(state_dir: Path) -> list[Path]:
    if not state_dir.is_dir():
        return []
    return sorted(
        p for p in state_dir.iterdir()
        if p.is_dir() and not p.is_symlink() and _BUILD_DIR_RE.match(p.name)
    )


def results_files(state_dir: Path) -> list[Path]:
    return [state_dir / name for name in RESULTS_FILES if (state_dir / name).is_file()]


def usage(state_dir: Path) -> tuple[int, int]:
    """(build output bytes, results bytes) for one state dir."""
    builds = sum(dir_size(p) for p in build_dirs(state_dir))
    results = sum(dir_size(p) for p in results_files(state_dir))
    return builds, results


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path)


def clear(state_dir: Path, include_results: bool = False) -> int:
    """Delete build output (and optionally the results history). Returns bytes freed."""
    targets = build_dirs(state_dir) + (results_files(state_dir) if include_results else [])
    freed = sum(dir_size(p) for p in targets)
    for path in targets:
        _remove(path)
    if include_results:
        try:
            state_dir.rmdir()  # only succeeds if nothing else is in there
        except OSError:
            pass
    return freed


def workspaces(root: Path | None = None) -> list[tuple[Path, int, int]]:
    """Every workspace state dir under the cache root, with its usage."""
    root = root or cache_root()
    if not root.is_dir():
        return []
    found = []
    for path in sorted(root.iterdir()):
        if path.is_dir() and not path.is_symlink():
            builds, results = usage(path)
            if builds or results or build_dirs(path) or results_files(path):
                found.append((path, builds, results))
    return found


def human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
