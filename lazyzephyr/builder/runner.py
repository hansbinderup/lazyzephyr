"""Run west commands one job at a time and stream their output."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


def west_command() -> list[str] | None:
    """How to start west: from PATH (the workspace dev shell), else this Python."""
    west = shutil.which("west")
    if west:
        return [west]
    try:
        import west  # noqa: F401
    except ImportError:
        return None
    return [sys.executable, "-m", "west"]


@dataclass
class Job:
    title: str
    config: str                    # config name
    action: str                    # build, pristine, flash, ...
    steps: list[list[str]]         # west arguments per step
    status: str = "queued"         # queued, running, passed, failed, cancelled, error
    returncode: int | None = None
    started: float | None = None
    finished: float | None = None
    step: int = 0
    lines: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float | None:
        if self.started is None:
            return None
        return (self.finished or time.time()) - self.started


class Runner:
    """Runs one Job at a time; each step must succeed before the next starts."""

    def __init__(self, cwd: Path, on_line: Callable[[Job, str], None],
                 on_change: Callable[[Job], None], west: list[str] | None = None):
        self.cwd = cwd
        self.on_line = on_line
        self.on_change = on_change
        self.west = west
        self.current: Job | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._cancelled = False

    @property
    def busy(self) -> bool:
        return self.current is not None

    async def run(self, job: Job) -> Job:
        west = self.west or west_command()
        if west is None:
            job.status = "error"
            self.on_line(job, "west not found: start lazyzephyr from the workspace dev shell")
            self.on_change(job)
            return job
        self.current, self._cancelled = job, False
        job.status, job.started = "running", time.time()
        self.on_change(job)
        env = dict(os.environ, CLICOLOR_FORCE="1")
        try:
            for i, args in enumerate(job.steps):
                job.step = i
                self.on_line(job, "$ west " + " ".join(args))
                try:
                    self._proc = await asyncio.create_subprocess_exec(
                        *west, *args, cwd=self.cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True)
                except OSError as exc:
                    job.status = "error"
                    self.on_line(job, f"could not start west: {exc}")
                    return job
                assert self._proc.stdout
                buf = b""
                while chunk := await self._proc.stdout.read(4096):
                    buf += chunk
                    *lines, buf = buf.replace(b"\r\n", b"\n").split(b"\n")
                    for line in lines:
                        self._emit(job, line)
                if buf:
                    self._emit(job, buf)
                job.returncode = await self._proc.wait()
                if self._cancelled:
                    job.status = "cancelled"
                    return job
                if job.returncode != 0:
                    job.status = "failed"
                    return job
            job.status = "passed"
            return job
        finally:
            self._proc = None
            job.finished = time.time()
            self.current = None
            self.on_change(job)

    def _emit(self, job: Job, raw: bytes) -> None:
        # Progress lines rewrite themselves with \r: keep the last state.
        line = raw.decode(errors="replace").split("\r")[-1]
        job.lines.append(line)
        self.on_line(job, line)

    def cancel(self) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return False
        self._cancelled = True
        try:
            os.killpg(proc.pid, signal.SIGINT)
        except ProcessLookupError:
            return True
        asyncio.get_running_loop().call_later(5, self._kill, proc)
        return True

    @staticmethod
    def _kill(proc) -> None:
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
