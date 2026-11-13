"""Parsing twister.json and keeping a persistent store of the latest results."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Worst first. Used to aggregate statuses across platforms / children.
SEVERITY = ["error", "failed", "blocked", "passed", "skipped", "not run", "filtered", "none"]
IGNORED_SUITE_STATUSES = {"filtered"}

_SUITE_RE = re.compile(r"Running TESTSUITE (\w+)")
_START_RE = re.compile(r"START - (\w+)")
# "Assertion failed at CMAKE_SOURCE_DIR/src/main.c:12: suite_test_x: (a not equal to b)"
# or, with CONFIG_ZTEST_ASSERT_VERBOSE=0, just "Assertion failed at <file>:<line>"
_ASSERT_RE = re.compile(r"Assertion failed at (.+?):(\d+)(?::\s*\w+:\s*(.*))?\s*$")
_RESULT_LINE_RE = re.compile(r"^\s*(FAIL|PASS|SKIP) - |^\s*START - |^=+$|^\s*$")
_CASE_END_RE = re.compile(r"\b(?:PASS|FAIL|SKIP) - (?:test_)?(\S+) in \d")
# written by the pytest harness each time it (re)starts the board for a test
_PYTEST_HEADER_RE = re.compile(r"^==== Test (\S+) started at ")
# where twister writes a test's console output, in order of preference
SUITE_LOGS = ("device.log", "handler.log")
MAX_CASE_LOG_LINES = 300


def split_case_logs(log: str | None, scenario: str) -> dict[str, str]:
    """Each test's own lines from a console log.

    ztest cases (keyed "suite.case", as parse_ztest_failures) run from
    "START - test_x" to "PASS/FAIL/SKIP - test_x". pytest tests are keyed
    "<scenario>.<test>" and split on the harness's per-test header, which only
    exists when the board is started per test (not with a session-scoped dut).
    """
    cases: dict[str, list[str]] = {}
    if not log:
        return {}
    lines = log.splitlines()
    suite = current = None
    for line in lines:
        if match := _SUITE_RE.search(line):
            suite, current = match.group(1), None
        elif (match := _START_RE.search(line)) and suite:
            current = f"{suite}.{match.group(1).replace('test_', '', 1)}"
            cases[current] = [line]
        elif current:
            cases[current].append(line)
            if (match := _CASE_END_RE.search(line)) and current.endswith("." + match.group(1)):
                current = None
    headers = [i for i, line in enumerate(lines) if _PYTEST_HEADER_RE.search(line)]
    if len(headers) > 1:
        for start, end in zip(headers, [*headers[1:], len(lines)]):
            name = _PYTEST_HEADER_RE.search(lines[start]).group(1)
            cases.setdefault(f"{scenario}.{name}", lines[start:end])
    return {case: "\n".join(lines[-MAX_CASE_LOG_LINES:]) for case, lines in cases.items()}


def find_suite_logs(outdir: Path) -> dict[tuple[str, str], Path]:
    """{(platform dir, scenario): console log} for every build dir in a twister outdir.

    Build dirs are <outdir>/<platform with / as _>/.../<scenario>/.
    """
    found: dict[tuple[str, str], Path] = {}
    for dirpath, dirnames, filenames in os.walk(outdir):
        here = Path(dirpath)
        log = next((name for name in SUITE_LOGS if name in filenames), None)
        if log is None and "build.log" not in filenames:
            dirnames.sort()
            continue
        dirnames.clear()  # a build dir: nothing more below it
        if log is not None and here != outdir:
            platform = here.relative_to(outdir).parts[0]
            found.setdefault((platform, here.name), here / log)
    return found


def attach_case_logs(results: list["SuiteResult"], outdir: Path) -> None:
    """Fill in each result's per-case output from the console logs in `outdir`."""
    logs = find_suite_logs(outdir)
    for result in results:
        path = logs.get((result.platform.replace("/", "_"), result.scenario))
        if path is None:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        result.case_logs = split_case_logs(text, result.scenario)


def parse_ztest_failures(log: str | None) -> dict[str, dict]:
    """Find the first failed assertion of each ztest case in a test log.

    Returns {case_id: {"file", "line", "message"}} with case ids as in
    twister.json ("suite.case"). `file` is as printed by ztest, which may
    start with CMAKE_SOURCE_DIR/, ZEPHYR_BASE/ or WEST_TOPDIR/.
    """
    failures: dict[str, dict] = {}
    if not log:
        return failures
    suite = case = None
    lines = log.splitlines()
    for index, line in enumerate(lines):
        if match := _SUITE_RE.search(line):
            suite, case = match.group(1), None
        elif match := _START_RE.search(line):
            case = match.group(1).replace("test_", "", 1)
        elif (match := _ASSERT_RE.search(line)) and suite and case:
            case_id = f"{suite}.{case}"
            if case_id in failures:
                continue
            message = [match.group(3) or ""]
            # zassert_*() user messages are printed on the following line.
            if index + 1 < len(lines) and not _RESULT_LINE_RE.search(lines[index + 1]):
                message.append(lines[index + 1].strip())
            failures[case_id] = {
                "file": match.group(1).strip(),
                "line": int(match.group(2)),
                "message": " — ".join(m for m in message if m),
            }
    return failures


def worst(statuses) -> str | None:
    best_index = None
    for status in statuses:
        if status is None:
            continue
        index = SEVERITY.index(status) if status in SEVERITY else len(SEVERITY)
        if best_index is None or index < best_index:
            best_index = index
    if best_index is None:
        return None
    return SEVERITY[best_index] if best_index < len(SEVERITY) else "none"


@dataclass
class SuiteResult:
    scenario: str
    platform: str
    status: str
    execution_time: float = 0.0
    build_time: float = 0.0
    finished_at: float = 0.0
    reason: str | None = None
    log: str | None = None
    cases: dict[str, str] = field(default_factory=dict)
    failures: dict[str, dict] = field(default_factory=dict)  # see parse_ztest_failures
    case_logs: dict[str, str] = field(default_factory=dict)  # see split_case_logs


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def parse_twister_json(path: Path) -> list[SuiteResult]:
    """Return suite results from a twister.json report (filtered suites dropped)."""
    data = json.loads(path.read_text())
    now = time.time()
    results = []
    for suite in data.get("testsuites", []):
        status = str(suite.get("status", "none"))
        if status in IGNORED_SUITE_STATUSES:
            continue
        cases = {
            str(tc.get("identifier")): str(tc.get("status", "none"))
            for tc in suite.get("testcases", [])
            if tc.get("identifier")
        }
        log = suite.get("log")
        results.append(
            SuiteResult(
                scenario=str(suite.get("name")),
                platform=str(suite.get("platform", "?")),
                status=status,
                execution_time=_float(suite.get("execution_time")),
                build_time=_float(suite.get("build_time")),
                finished_at=now,
                reason=suite.get("reason"),
                log=log,
                cases=cases,
                failures=parse_ztest_failures(log) if status in ("failed", "error") else {},
            )
        )
    return results


_ANSI = {"passed": "32", "failed": "31", "error": "1;31", "blocked": "31"}
_MARK = {"passed": "✔", "failed": "✘", "error": "!", "blocked": "⊘"}


def _colored(status: str, text: str) -> str:
    return f"\x1b[{_ANSI.get(status, '2')}m{text}\x1b[0m"


def results_lines(results: list[SuiteResult]) -> list[str]:
    """Every scenario's result and its cases, for the Output panel."""
    lines = [f"── results: {len(results)} scenario(s) ──"]
    for result in sorted(results, key=lambda r: (r.scenario, r.platform)):
        statuses = list(result.cases.values())
        passed = statuses.count("passed")
        mark = _MARK.get(result.status, "○")
        line = f"{mark} {result.scenario}  {result.platform}  {result.status}"
        if statuses:
            line += f"  {passed}/{len(statuses)} cases"
        if result.execution_time:
            line += f"  run {result.execution_time:.2f}s"
        lines.append(_colored(result.status, line))
        if result.reason and result.status != "passed":
            lines.append(_colored(result.status, f"    reason: {result.reason}"))
        for case_id, status in result.cases.items():
            case = f"    {_MARK.get(status, '○')} {case_id}  {status}"
            failure = result.failures.get(case_id)
            if failure:
                case += f"  {failure['file']}:{failure['line']}"
                if failure["message"]:
                    case += f"  {failure['message']}"
            lines.append(_colored(status, case))
            for log_line in result.case_logs.get(case_id, "").splitlines():
                lines.append(f"\x1b[2m        │\x1b[0m {log_line}")
    return lines


class ResultStore:
    """Latest result per (scenario, platform), persisted as JSON."""

    def __init__(self, path: Path | None):
        self.path = path
        self._data: dict[str, dict[str, SuiteResult]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text())
            for scenario, platforms in raw.get("scenarios", {}).items():
                self._data[scenario] = {p: SuiteResult(**r) for p, r in platforms.items()}
        except (OSError, ValueError, TypeError):
            # A corrupt state file just means we start without history.
            self._data = {}

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            "scenarios": {
                s: {p: asdict(r) for p, r in platforms.items()}
                for s, platforms in self._data.items()
            }
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=1))
        tmp.replace(self.path)

    def merge(self, results: list[SuiteResult], only_cases: set[str] | None = None) -> None:
        """Merge new results.

        `only_cases`: when a run was filtered down to specific ztest cases,
        only those case results are updated, and the suite status is derived
        from the merged cases so that skipped siblings don't hide old results.
        """
        for result in results:
            platforms = self._data.setdefault(result.scenario, {})
            previous = platforms.get(result.platform)
            if only_cases is not None and previous is not None:
                cases = dict(previous.cases)
                cases.update({k: v for k, v in result.cases.items() if k in only_cases})
                result.cases = cases
                failures = {k: v for k, v in previous.failures.items() if k not in only_cases}
                failures.update({k: v for k, v in result.failures.items() if k in only_cases})
                result.failures = failures
                logs = {k: v for k, v in previous.case_logs.items() if k not in only_cases}
                logs.update({k: v for k, v in result.case_logs.items() if k in only_cases})
                result.case_logs = logs
                if result.status not in ("error",):
                    result.status = worst(cases.values()) or result.status
            platforms[result.platform] = result

    def for_scenario(self, scenario: str) -> dict[str, SuiteResult]:
        return self._data.get(scenario, {})

    def scenario_status(self, scenario: str) -> str | None:
        return worst(r.status for r in self.for_scenario(scenario).values())

    def case_status(self, scenario: str, case_id: str) -> str | None:
        return worst(r.cases.get(case_id) for r in self.for_scenario(scenario).values())

    def failed_scenarios(self) -> list[str]:
        return [s for s in self._data if self.scenario_status(s) in ("failed", "error", "blocked")]

    def clear(self) -> None:
        self._data = {}
