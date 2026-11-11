"""Choosing which folders lazyzephyr discovers tests in.

The selection made in the folder browser is remembered per directory in the
state dir (`folders.json`), so nothing is written into the repo. On start the
test roots come from, in order: `-T`, `roots` in `.lazyzephyr.toml`, the saved
selection, or else the folder browser opens (the first time in a directory).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .discovery import TESTCASE_FILES, _skip_dir

SAVED_NAME = "folders.json"

# Never offered in the browser: hidden folders and twister/build output.
_HIDE_PREFIXES = (".", "twister-out")


def list_dirs(path: Path) -> list[Path]:
    """Sub-directories shown in the browser, sorted by name."""
    try:
        entries = [e for e in os.scandir(path) if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []
    return sorted(
        (Path(e.path) for e in entries if not e.name.startswith(_HIDE_PREFIXES)),
        key=lambda p: p.name.lower(),
    )


def count_tests(top: Path) -> dict[Path, int]:
    """Number of testcase.yaml / sample.yaml files in each directory below `top`.

    Counts include sub-directories. `top` is always scanned; below it the same
    directories discovery skips are skipped (they're missing from the result).
    """
    counts: dict[Path, int] = {}
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
        here = sum(name in filenames for name in TESTCASE_FILES)
        path = Path(dirpath)
        counts.setdefault(path, 0)
        if here:
            # Walk up to `top`, adding to every ancestor.
            while True:
                counts[path] = counts.get(path, 0) + here
                if path == top or path.parent == path:
                    break
                path = path.parent
    return counts


def normalize(paths: list[Path]) -> list[Path]:
    """Resolve, de-duplicate and drop folders already inside another chosen folder."""
    resolved = sorted({p.resolve() for p in paths}, key=lambda p: p.parts)
    kept: list[Path] = []
    for path in resolved:
        if not any(k == path or k in path.parents for k in kept):
            kept.append(path)
    return kept


def _saved_file(state_dir: Path) -> Path:
    return state_dir / SAVED_NAME


def _read_all(state_dir: Path) -> dict[str, list[str]]:
    try:
        data = json.loads(_saved_file(state_dir).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        k: v for k, v in data.items()
        if isinstance(k, str) and isinstance(v, list) and all(isinstance(i, str) for i in v)
    }


def load_saved(state_dir: Path, cwd: Path) -> list[Path] | None:
    """Folders saved for `cwd`, minus any that no longer exist; None if never saved
    (or nothing saved still exists)."""
    saved = _read_all(state_dir).get(str(cwd.resolve()))
    if saved is None:
        return None
    existing = [Path(p) for p in saved if Path(p).is_absolute() and Path(p).is_dir()]
    return normalize(existing) or None


def save(state_dir: Path, cwd: Path, roots: list[Path]) -> None:
    data = _read_all(state_dir)
    data[str(cwd.resolve())] = [str(p) for p in normalize(roots)]
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = _saved_file(state_dir).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(_saved_file(state_dir))
