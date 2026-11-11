"""Run GIL-holding work in a child Python process.

Setting up the file watcher (one inotify watch per directory, done while holding
the GIL) froze the UI for big test roots like zephyr/tests, even from a thread.
A child process can't.

The child is started with `-I` (ignores PYTHONPATH and the user site, see
launcher.py) and gets our sys.path as the first line of stdin, as JSON, together
with the function's arguments. This module must only import the standard library.
"""

from __future__ import annotations

import json
import sys


def command(module: str, func: str) -> list[str]:
    """Command running `module.func(request)` with the request read from stdin."""
    code = (
        "import json, sys; request = json.loads(sys.stdin.readline()); "
        "sys.path[:] = request.pop('path'); "
        f"from {module} import {func}; {func}(request)"
    )
    return [sys.executable, "-I", "-c", code]


def request(**fields) -> bytes:
    """First line of the child's stdin."""
    return json.dumps({"path": sys.path, **fields}).encode() + b"\n"


def last_error_line(stderr: bytes, returncode: int | None) -> str:
    lines = stderr.decode(errors="replace").strip().splitlines()
    return lines[-1] if lines else f"exit code {returncode}"
