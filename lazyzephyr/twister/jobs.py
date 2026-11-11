"""Twister run targets, command construction and the job queue."""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .discovery import Discovery, ZtestCase
from .results import SuiteResult, parse_twister_json

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# "INFO    - 3/12 native_sim/native/64   lib.foo.native_sim   PASSED (native 0.01s <host>)"
RESULT_LINE_RE = re.compile(
    r"\b(\d+)/(\d+)\s+(\S+)\s+(\S+)\s+(PASSED|FAILED|ERROR|SKIPPED|NOT RUN|FILTERED|BLOCKED)\b"
)
# Non-verbose failure line: "ERROR   - native_sim/native/64   lib.foo.native_sim   FAILED: reason"
FAILURE_LINE_RE = re.compile(r"-\s+(\S+)\s+(\S+)\s+(FAILED|ERROR):")
# "INFO    - Total complete:    4/  12  33%  ..."
PROGRESS_RE = re.compile(r"Total complete:\s*(\d+)\s*/\s*(\d+)")

MAX_LOG_LINES = 20_000


@dataclass(frozen=True)
class NodeRef:
    """What a tree node points at."""

    kind: str  # "folder" | "scenario" | "case"
    path: Path
    scenario: str | None = None
    case: ZtestCase | None = None

    @property
    def key(self) -> str:
        if self.kind == "folder":
            return f"d:{self.path}"
        if self.kind == "scenario":
            return f"s:{self.scenario}"
        assert self.case is not None
        return f"c:{self.scenario}:{self.case.id}"

    @property
    def title(self) -> str:
        if self.kind == "folder":
            return f"{self.path.name}/"
        if self.kind == "scenario":
            return str(self.scenario)
        assert self.case is not None
        return f"{self.scenario} › {self.case.id}"


@dataclass
class RunOptions:
    platforms: list[str] = field(default_factory=list)
    integration: bool = True
    inline_logs: bool = True
    verbose: bool = True  # -v: twister prints one line per finished scenario (live status)
    extra_args: list[str] = field(default_factory=list)
    jobs: int | None = None  # twister -j; None = twister's default


@dataclass
class RunTarget:
    title: str
    testsuite_roots: list[Path]
    scenarios: list[str]  # -s arguments; empty = everything under the roots
    test_filters: list[str]  # ztest -test=... filters (native targets only)
    covered: set[str]  # scenario names whose status this run affects
    only_cases: set[str] | None = None  # case ids, when filtered to cases

    def signature(self) -> tuple:
        return (
            tuple(sorted(map(str, self.testsuite_roots))),
            tuple(sorted(self.scenarios)),
            tuple(sorted(self.test_filters)),
        )


def job_title(refs: list[NodeRef], shown: int = 2) -> str:
    """'drivers.beta', or 'drivers.beta, lib/ +3' for several nodes."""
    titles = [r.title for r in refs]
    head = ", ".join(titles[:shown])
    return head if len(titles) <= shown else f"{head} +{len(titles) - shown}"


def display_command(cmd: list[str], cwd: Path) -> str:
    """The command as shown in the output panel: relative paths, no -O noise."""
    shown: list[str] = []
    skip_next = False
    for arg in cmd:
        if skip_next:
            skip_next = False
            continue
        if arg == "-O":
            skip_next = True
            continue
        path = Path(arg)
        if path.is_absolute():
            try:
                arg = str(path.relative_to(cwd)) or "."
            except ValueError:
                pass
        shown.append(shlex.quote(arg))
    return " ".join(shown)


def make_target(refs: list[NodeRef], discovery: Discovery) -> RunTarget:
    """Turn selected tree nodes into one twister invocation."""
    if not refs:
        raise ValueError("nothing selected")
    by_name = discovery.by_name()
    title = job_title(refs)

    # Only folders: plain -T, so twister also picks up anything we failed to parse.
    if all(r.kind == "folder" for r in refs):
        roots = list(dict.fromkeys(r.path for r in refs))
        covered = {s.name for r in roots for s in discovery.under(r)}
        return RunTarget(title, roots, [], [], covered)

    scenario_names: list[str] = []
    filters: list[str] = []
    case_ids: set[str] = set()
    can_filter = True
    for ref in refs:
        if ref.kind == "folder":
            can_filter = False
            scenario_names.extend(s.name for s in discovery.under(ref.path))
        elif ref.kind == "scenario":
            scenario_names.append(str(ref.scenario))
            suites = by_name[ref.scenario].suites if ref.scenario in by_name else []
            if suites:
                filters.extend(f"{suite}::*" for suite in suites)
            else:
                can_filter = False
        else:
            assert ref.case is not None
            scenario_names.append(str(ref.scenario))
            filters.append(ref.case.filter)
            case_ids.add(ref.case.id)

    scenario_names = list(dict.fromkeys(scenario_names))
    has_cases = any(r.kind == "case" for r in refs)
    if not has_cases or not can_filter:
        # Whole scenarios: no binary-level filtering needed.
        filters, only = [], None
    else:
        only = case_ids if all(r.kind == "case" for r in refs) else None
        filters = list(dict.fromkeys(filters))

    roots = list(
        dict.fromkeys(by_name[n].directory for n in scenario_names if n in by_name)
    )
    return RunTarget(title, roots, scenario_names, filters, set(scenario_names), only)


def build_command(
    base: list[str], target: RunTarget, options: RunOptions, outdir: Path, jobs: int | None
) -> list[str]:
    cmd = list(base)
    for root in target.testsuite_roots:
        cmd += ["-T", str(root)]
    for scenario in target.scenarios:
        cmd += ["-s", scenario]
    for platform in options.platforms:
        cmd += ["-p", platform]
    if options.integration:
        cmd.append("-G")
    if options.inline_logs:
        cmd.append("-i")
    if options.verbose:
        cmd.append("-v")
    if jobs:
        cmd += ["-j", str(jobs)]
    cmd += ["-O", str(outdir)]
    # Without this twister renames the previous outdir to run.1, run.2, ... so the
    # cache grows with every run (and twister gives up after 99).
    if not any(a in ("-n", "--no-clean") for a in options.extra_args):
        cmd.append("--clobber-output")
    cmd += options.extra_args
    if target.test_filters:
        cmd += ["--", "-test=" + ",".join(target.test_filters)]
    return cmd


@dataclass
class Job:
    id: int
    target: RunTarget
    options: RunOptions
    state: str = "queued"  # queued | running | passed | failed | cancelled | error
    command: list[str] = field(default_factory=list)
    outdir: Path | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    returncode: int | None = None
    done: int = 0
    total: int = 0
    lines: list[str] = field(default_factory=list)
    live_status: dict[str, str] = field(default_factory=dict)  # scenario -> status
    results: list[SuiteResult] = field(default_factory=list)
    process: asyncio.subprocess.Process | None = None

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at


class JobManager:
    """Runs twister jobs one at a time from a FIFO queue.

    Only one twister process runs at once (twister already parallelises its
    builds internally), so a run never competes with another for CPU. Every
    job reuses the same output directory, which twister cleans before each run.
    """

    def __init__(
        self,
        twister_cmd: list[str],
        state_dir: Path,
        cwd: Path,
        on_change: Callable[[Job], None] | None = None,
        on_line: Callable[[Job, str], None] | None = None,
        on_finished: Callable[[Job], None] | None = None,
    ):
        self.twister_cmd = twister_cmd
        self.state_dir = state_dir
        self.cwd = cwd
        self.on_change = on_change or (lambda job: None)
        self.on_line = on_line or (lambda job, line: None)
        self.on_finished = on_finished or (lambda job: None)
        self.jobs: list[Job] = []  # every job, in submission order (history)
        self.queue: list[Job] = []  # waiting jobs, in execution order
        self.current: Job | None = None  # set until its process has fully exited
        self._next_id = 1
        self._task: asyncio.Task | None = None

    # -- queue -------------------------------------------------------------

    def submit(self, target: RunTarget, options: RunOptions) -> Job | None:
        """Append a run to the queue. Returns None if an identical run is already queued."""
        signature = target.signature()
        if any(job.target.signature() == signature for job in self.queue):
            return None
        job = Job(self._next_id, target, options)
        self._next_id += 1
        self.jobs.append(job)
        self.queue.append(job)
        self.on_change(job)
        self._start_next()
        return job

    def queued(self) -> list[Job]:
        return list(self.queue)

    def running(self) -> list[Job]:
        return [self.current] if self.current is not None else []

    def position(self, job: Job) -> int | None:
        """1-based place in the queue, or None if not queued."""
        return self.queue.index(job) + 1 if job in self.queue else None

    def move(self, job: Job, delta: int) -> bool:
        """Move a queued job earlier (negative) or later (positive) in the queue."""
        if job not in self.queue:
            return False
        index = self.queue.index(job)
        new_index = max(0, min(len(self.queue) - 1, index + delta))
        if new_index == index:
            return False
        self.queue.insert(new_index, self.queue.pop(index))
        self.on_change(job)
        return True

    def _start_next(self) -> None:
        if self.current is not None or not self.queue:
            return
        job = self.queue.pop(0)
        job.state = "running"
        self.current = job
        self._task = asyncio.get_running_loop().create_task(self._run(job))

    # -- execution ---------------------------------------------------------

    async def _run(self, job: Job) -> None:
        job.outdir = self.state_dir / "run"
        job.command = build_command(
            self.twister_cmd, job.target, job.options, job.outdir, job.options.jobs
        )
        job.started_at = time.time()
        if job.state == "cancelled":  # cancelled before the task got to run
            self._finish(job, "cancelled")
            return
        self._emit(job, "$ " + display_command(job.command, self.cwd))
        self.on_change(job)

        env = dict(os.environ, PYTHONUNBUFFERED="1")
        try:
            job.process = await asyncio.create_subprocess_exec(
                *job.command,
                cwd=self.cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,  # own process group, so cancel kills builds too
            )
        except OSError as exc:
            self._emit(job, f"failed to start twister: {exc}")
            self._emit(
                job,
                "hint: start lazyzephyr from the workspace dev shell (e.g. `nix develop`) "
                "or pass --twister '<command>'",
            )
            self._finish(job, "error")
            return

        assert job.process.stdout is not None
        buffer = b""
        while True:
            chunk = await job.process.stdout.read(65536)
            if not chunk:
                break
            buffer += chunk
            # Twister redraws progress with \r; treat it as a line break.
            *complete, buffer = re.split(rb"\r\n|\n|\r", buffer)
            for raw in complete:
                self._handle_line(job, raw.decode(errors="replace"))
        if buffer:
            self._handle_line(job, buffer.decode(errors="replace"))

        job.returncode = await job.process.wait()
        report = job.outdir / "twister.json"
        if job.state != "cancelled" and report.is_file():
            try:
                job.results = parse_twister_json(report)
            except (OSError, ValueError) as exc:
                self._emit(job, f"could not parse {report}: {exc}")
        if job.state == "cancelled":
            self._finish(job, "cancelled")
        elif job.returncode == 0:
            self._finish(job, "passed")
        else:
            self._finish(job, "failed")

    def _handle_line(self, job: Job, line: str) -> None:
        plain = ANSI_RE.sub("", line)
        if not plain.strip():
            return
        progress = PROGRESS_RE.search(plain)
        if progress:
            job.done, job.total = int(progress.group(1)), int(progress.group(2))
            self.on_change(job)
            return  # progress redraw lines are noise in the log
        result = RESULT_LINE_RE.search(plain)
        if result:
            job.done, job.total = int(result.group(1)), int(result.group(2))
            job.live_status[result.group(4)] = result.group(5).lower()
            self.on_change(job)
        else:
            failure = FAILURE_LINE_RE.search(plain)
            if failure:
                job.live_status[failure.group(2)] = failure.group(3).lower()
                self.on_change(job)
        self._emit(job, line)

    def _emit(self, job: Job, line: str) -> None:
        job.lines.append(line)
        if len(job.lines) > MAX_LOG_LINES:
            del job.lines[: len(job.lines) - MAX_LOG_LINES]
        self.on_line(job, line)

    def _finish(self, job: Job, state: str) -> None:
        job.state = state
        job.finished_at = time.time()
        job.process = None
        self._emit(job, f"── job #{job.id} {state} in {job.elapsed:.1f}s ──")
        if job is self.current:
            self.current = None
        self.on_finished(job)
        self.on_change(job)
        self._start_next()

    # -- control -----------------------------------------------------------

    def cancel(self, job: Job) -> None:
        """Remove a queued job, or stop the running one (the queue then continues)."""
        if job in self.queue:
            self.queue.remove(job)
            job.state = "cancelled"
            job.finished_at = time.time()
            self.on_finished(job)
            self.on_change(job)
        elif job is self.current and job.state == "running":
            job.state = "cancelled"
            if job.process is not None:
                self._emit(job, "cancelling…")
                self._signal(job, signal.SIGINT)
                asyncio.get_running_loop().call_later(5, self._signal, job, signal.SIGKILL)

    def clear_queue(self) -> None:
        """Cancel every queued job; the running one keeps going."""
        for job in list(self.queue):
            self.cancel(job)

    def cancel_all(self) -> None:
        self.clear_queue()
        if self.current is not None:
            self.cancel(self.current)

    @staticmethod
    def _signal(job: Job, sig: int) -> None:
        if job.process is None or job.process.returncode is not None:
            return
        try:
            os.killpg(job.process.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def clear_finished(self) -> None:
        self.jobs = [j for j in self.jobs if j.active]

    def shutdown(self) -> None:
        if self.current is not None:
            self._signal(self.current, signal.SIGKILL)
