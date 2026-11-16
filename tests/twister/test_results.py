import json
from pathlib import Path

from lazyzephyr.twister.results import ResultStore, SuiteResult, parse_twister_json, worst


def test_worst():
    assert worst(["passed", "skipped", "failed"]) == "failed"
    assert worst([None, "passed"]) == "passed"
    assert worst([]) is None


def test_parse_twister_json(tmp_path: Path):
    report = tmp_path / "twister.json"
    report.write_text(
        json.dumps(
            {
                "testsuites": [
                    {
                        "name": "a.b",
                        "platform": "native_sim",
                        "status": "passed",
                        "execution_time": "0.5",
                        "testcases": [{"identifier": "s.one", "status": "passed"}],
                    },
                    {"name": "a.c", "platform": "qemu_x86", "status": "filtered"},
                ]
            }
        )
    )
    results = parse_twister_json(report)
    assert [(r.scenario, r.status) for r in results] == [("a.b", "passed")]
    assert results[0].execution_time == 0.5
    assert results[0].cases == {"s.one": "passed"}


def test_store_roundtrip_and_aggregation(tmp_path: Path):
    path = tmp_path / "state" / "results.json"
    store = ResultStore(path)
    store.merge(
        [
            SuiteResult("a.b", "native_sim", "passed", cases={"s.one": "passed"}),
            SuiteResult("a.b", "qemu_x86", "failed", cases={"s.one": "failed"}),
        ]
    )
    store.save()
    again = ResultStore(path)
    assert again.scenario_status("a.b") == "failed"
    assert again.case_status("a.b", "s.one") == "failed"
    assert again.failed_scenarios() == ["a.b"]


def test_case_only_merge_keeps_sibling_results(tmp_path: Path):
    store = ResultStore(None)
    store.merge(
        [SuiteResult("a.b", "native_sim", "failed", cases={"s.one": "passed", "s.two": "failed"})]
    )
    # Re-running only s.two: twister reports s.one as skipped — must not clobber it.
    store.merge(
        [SuiteResult("a.b", "native_sim", "passed", cases={"s.one": "skipped", "s.two": "passed"})],
        only_cases={"s.two"},
    )
    result = store.for_scenario("a.b")["native_sim"]
    assert result.cases == {"s.one": "passed", "s.two": "passed"}
    assert result.status == "passed"


def test_corrupt_state_file_is_ignored(tmp_path: Path):
    path = tmp_path / "results.json"
    path.write_text("{not json")
    assert ResultStore(path).scenario_status("x") is None


DATA = Path(__file__).parent / "data"


def test_parse_ztest_failures_from_real_log():
    from lazyzephyr.twister.results import parse_ztest_failures

    failures = parse_ztest_failures((DATA / "ztest_fail.log").read_text())
    assert failures == {
        "demo_suite.fails": {
            "file": "CMAKE_SOURCE_DIR/src/main.c",
            "line": 12,
            "message": "(value not equal to 3) — value was 2",
        }
    }


def test_parse_ztest_failures_non_verbose_asserts():
    from lazyzephyr.twister.results import parse_ztest_failures

    log = "Running TESTSUITE s\nSTART - test_x\n\n    Assertion failed at /abs/t.c:7\n FAIL - test_x\n"
    assert parse_ztest_failures(log) == {"s.x": {"file": "/abs/t.c", "line": 7, "message": ""}}
    assert parse_ztest_failures(None) == {}


def test_case_only_merge_keeps_other_failures():
    store = ResultStore(None)
    store.merge(
        [
            SuiteResult(
                "a.b",
                "p",
                "failed",
                cases={"s.one": "failed", "s.two": "failed"},
                failures={"s.one": {"line": 1}, "s.two": {"line": 2}},
            )
        ]
    )
    store.merge(
        [SuiteResult("a.b", "p", "passed", cases={"s.two": "passed"})], only_cases={"s.two"}
    )
    assert store.for_scenario("a.b")["p"].failures == {"s.one": {"line": 1}}


def test_results_lines_show_cases_and_failures():
    import re

    from lazyzephyr.twister.results import results_lines

    failed = SuiteResult(
        "lib.x",
        "frdm_k64f",
        "failed",
        execution_time=4.2,
        reason="Testsuite failed",
        cases={"s.ok": "passed", "s.bad": "failed", "s.skip": "skipped"},
        failures={"s.bad": {"file": "src/main.cpp", "line": 12, "message": "a not equal to b"}},
    )
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in results_lines([failed])]
    assert plain == [
        "── results: 1 scenario(s) ──",
        "✘ lib.x  frdm_k64f  failed  1/3 cases  run 4.20s",
        "    reason: Testsuite failed",
        "    ✔ s.ok  passed",
        "    ✘ s.bad  failed  src/main.cpp:12  a not equal to b",
        "    ○ s.skip  skipped",
    ]


def test_split_case_logs_ztest_from_real_log():
    from lazyzephyr.twister.results import split_case_logs

    logs = split_case_logs((DATA / "ztest_fail.log").read_text(), "demo.scenario")
    assert set(logs) == {"demo_suite.fails", "demo_suite.passes"}
    assert logs["demo_suite.fails"].splitlines() == [
        "START - test_fails",
        "",
        "    Assertion failed at CMAKE_SOURCE_DIR/src/main.c:12: demo_suite_test_fails: "
        "(value not equal to 3)",
        "value was 2",
        " FAIL - test_fails in 0.000 seconds",
    ]
    assert logs["demo_suite.passes"].splitlines() == [
        "START - test_passes",
        " PASS - test_passes in 0.000 seconds",
    ]
    assert split_case_logs(None, "x") == {}


def test_split_case_logs_pytest_headers_and_cap():
    from lazyzephyr.twister.results import MAX_CASE_LOG_LINES, split_case_logs

    log = (
        "\n==== Test test_a started at 2026-11-13 ====\nboot\na says hi\n"
        "==== Test test_b started at 2026-11-13 ====\nb says hi\n"
    )
    assert split_case_logs(log, "lib.x") == {
        "lib.x.test_a": "==== Test test_a started at 2026-11-13 ====\nboot\na says hi",
        "lib.x.test_b": "==== Test test_b started at 2026-11-13 ====\nb says hi",
    }
    # One header (dut started once for the session): can't tell the tests apart.
    assert split_case_logs("==== Test test_a started at x ====\nall\n", "lib.x") == {}

    long = "Running TESTSUITE s\nSTART - test_x\n" + "".join(f"l{i}\n" for i in range(500))
    assert len(split_case_logs(long, "y")["s.x"].splitlines()) == MAX_CASE_LOG_LINES


def test_attach_case_logs_finds_build_dirs(tmp_path: Path):
    from lazyzephyr.twister.results import attach_case_logs, find_suite_logs

    def build(platform: str, rel: str, log: str | None, text: str = "") -> Path:
        directory = tmp_path / platform / rel
        (directory / "zephyr").mkdir(parents=True)
        (directory / "build.log").write_text("built\n")
        if log:
            (directory / log).write_text(text)
        return directory

    native = build(
        "native_sim_native_64",
        "zephyr/tests/lib/a.b",
        "handler.log",
        "Running TESTSUITE s\nSTART - test_one\nhi\n PASS - test_one in 0.1 seconds\n",
    )
    board = build("frdm_k64f", "tests/lib/a.b", "device.log", "device output\n")
    (board / "handler.log").write_text("handler output\n")
    build("qemu_x86", "a.c", None)  # built only: no console log

    assert find_suite_logs(tmp_path) == {
        ("frdm_k64f", "a.b"): board / "device.log",  # device.log preferred
        ("native_sim_native_64", "a.b"): native / "handler.log",
    }
    results = [
        SuiteResult("a.b", "native_sim/native/64", "passed", cases={"s.one": "passed"}),
        SuiteResult("a.c", "qemu_x86", "passed"),
    ]
    attach_case_logs(results, tmp_path)
    assert results[0].case_logs == {
        "s.one": "START - test_one\nhi\n PASS - test_one in 0.1 seconds"
    }
    assert results[1].case_logs == {}


def test_case_only_merge_keeps_other_case_logs():
    store = ResultStore(None)
    store.merge(
        [
            SuiteResult(
                "a.b",
                "p",
                "passed",
                cases={"s.one": "passed", "s.two": "passed"},
                case_logs={"s.one": "old one", "s.two": "old two"},
            )
        ]
    )
    store.merge(
        [
            SuiteResult(
                "a.b",
                "p",
                "passed",
                cases={"s.two": "passed"},
                case_logs={"s.two": "new two", "s.one": "ignored"},
            )
        ],
        only_cases={"s.two"},
    )
    assert store.for_scenario("a.b")["p"].case_logs == {"s.one": "old one", "s.two": "new two"}


def test_old_results_file_without_case_logs_loads(tmp_path: Path):
    path = tmp_path / "results.json"
    path.write_text(
        json.dumps(
            {"scenarios": {"a.b": {"p": {"scenario": "a.b", "platform": "p", "status": "passed"}}}}
        )
    )
    assert ResultStore(path).for_scenario("a.b")["p"].case_logs == {}
