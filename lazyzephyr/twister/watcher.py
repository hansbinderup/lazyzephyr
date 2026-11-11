"""Watch the test roots for changes, in a child process.

watchfiles adds one inotify watch per directory while holding the GIL, which
froze the UI for big roots like zephyr/tests (~0.3s), even from a thread. The
child prints each debounced batch of changed paths as a JSON line.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from collections.abc import AsyncIterator
from pathlib import Path

from . import child

IGNORE_PARTS = {".git", ".lazyzephyr", "build", "__pycache__", ".venv"}
IGNORE_SUFFIXES = (".swp", ".swx", ".tmp", "~", ".pyc")
DEBOUNCE_MS = 600


class WatchError(RuntimeError):
    pass


def keep(_change, path: str) -> bool:
    """watchfiles filter: skip VCS/build output and editor temp files."""
    parts = Path(path).parts
    if any(p in IGNORE_PARTS or p.startswith("twister-out") for p in parts):
        return False
    return not path.endswith(IGNORE_SUFFIXES)


def _child_main(request: dict) -> None:
    from watchfiles import watch

    # Stop when the parent closes our stdin (or dies).
    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
    # yield_on_timeout: the first (empty) batch means the watches are in place.
    ready = False
    for changes in watch(*request["paths"], watch_filter=keep, debounce=DEBOUNCE_MS,
                         stop_event=stop, yield_on_timeout=True, rust_timeout=200):
        if changes:
            print(json.dumps(sorted({p for _, p in changes})), flush=True)
        elif not ready:
            ready = True
            print(json.dumps("ready"), flush=True)


async def watch_changes(paths: list[Path], on_ready=None) -> AsyncIterator[list[Path]]:
    """Yield the changed paths under `paths`, batch by batch, until cancelled.

    `on_ready()` is called once the watches are in place."""
    proc = await asyncio.create_subprocess_exec(
        *child.command("lazyzephyr.twister.watcher", "_child_main"),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        proc.stdin.write(child.request(paths=[str(p) for p in paths]))
        await proc.stdin.drain()  # stdin stays open: closing it stops the child
        while line := await proc.stdout.readline():
            batch = json.loads(line)
            if batch == "ready":
                if on_ready is not None:
                    on_ready()
                continue
            yield [Path(p) for p in batch]
        err = await proc.stderr.read()
        await proc.wait()
        raise WatchError(child.last_error_line(err, proc.returncode))
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
