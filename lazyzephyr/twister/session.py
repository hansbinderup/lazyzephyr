"""The Tests tab's settings as last used, so the next start picks up where you left off.

Saved per workspace in the state dir (`session.json`), so nothing is written into
the repo. On start each setting comes from, in order: the command line, the saved
session, `.lazyzephyr.toml`, or the default.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path

SAVED_NAME = "session.json"


@dataclass
class Session:
    platforms: list[str] | None = None  # p
    case_results: bool | None = None  # V
    integration: bool | None = None  # g (-G)
    auto_run: bool | None = None  # w
    platform_only: bool | None = None  # P
    board_choice: str | None = None  # board picked last when several are enabled


_BOOLS = ("case_results", "integration", "auto_run", "platform_only")


def load(state_dir: Path) -> Session:
    """The saved session; a missing, broken or malformed setting is left unset."""
    try:
        data = json.loads((state_dir / SAVED_NAME).read_text())
    except (OSError, ValueError):
        return Session()
    if not isinstance(data, dict):
        return Session()
    session = Session()
    platforms = data.get("platforms")
    if isinstance(platforms, list) and all(isinstance(p, str) and p for p in platforms):
        session.platforms = platforms
    for key in _BOOLS:
        if isinstance(data.get(key), bool):
            setattr(session, key, data[key])
    if isinstance(data.get("board_choice"), str) and data["board_choice"]:
        session.board_choice = data["board_choice"]
    return session


def save(state_dir: Path, session: Session) -> None:
    data = {
        f.name: getattr(session, f.name)
        for f in fields(session)
        if getattr(session, f.name) is not None
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = (state_dir / SAVED_NAME).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(state_dir / SAVED_NAME)
