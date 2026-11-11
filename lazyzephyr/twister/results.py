"""Parsing twister.json and keeping a persistent store of the latest results."""

from __future__ import annotations

import json
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
