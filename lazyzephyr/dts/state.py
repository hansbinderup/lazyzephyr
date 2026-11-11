"""Remember the last target per directory."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class TargetChoice:
    """What the user picked: an existing build, or a board (+ app, shields)."""

    build_dir: str | None = None
    board: str | None = None
    app: str | None = None
    shields: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.build_dir:
            return f"build {self.build_dir}"
        extra = f" + {', '.join(self.shields)}" if self.shields else ""
        return f"{self.board}{extra}" + (f"  ({self.app})" if self.app else "")


def state_file() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "lazyzephyr" / "targets.json"


def _load() -> dict:
    try:
        return json.loads(state_file().read_text())
    except (OSError, ValueError):
        return {}


def load_choice(cwd: Path) -> TargetChoice | None:
    raw = _load().get(str(cwd))
    if not isinstance(raw, dict):
        return None
    try:
        return TargetChoice(**raw)
    except TypeError:
        return None


def save_choice(cwd: Path, choice: TargetChoice) -> None:
    data = _load()
    data[str(cwd)] = asdict(choice)
    path = state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass
