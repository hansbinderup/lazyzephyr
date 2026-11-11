import json
from pathlib import Path

from lazyzephyr.twister.results import ResultStore, SuiteResult, parse_twister_json, worst


def test_worst():
    assert worst(["passed", "skipped", "failed"]) == "failed"
    assert worst([None, "passed"]) == "passed"
    assert worst([]) is None


def test_parse_twister_json(tmp_path: Path):
    report = tmp_path / "twister.json"
    report.write_text(json.dumps({"testsuites": [
        {"name": "a.b", "platform": "native_sim", "status": "passed", "execution_time": "0.5",
         "testcases": [{"identifier": "s.one", "status": "passed"}]},
        {"name": "a.c", "platform": "qemu_x86", "status": "filtered"},
    ]}))
    results = parse_twister_json(report)
    assert [(r.scenario, r.status) for r in results] == [("a.b", "passed")]
    assert results[0].execution_time == 0.5
    assert results[0].cases == {"s.one": "passed"}


def test_store_roundtrip_and_aggregation(tmp_path: Path):
    path = tmp_path / "state" / "results.json"
    store = ResultStore(path)
    store.merge([
        SuiteResult("a.b", "native_sim", "passed", cases={"s.one": "passed"}),
        SuiteResult("a.b", "qemu_x86", "failed", cases={"s.one": "failed"}),
    ])
    store.save()
    again = ResultStore(path)
    assert again.scenario_status("a.b") == "failed"
    assert again.case_status("a.b", "s.one") == "failed"
    assert again.failed_scenarios() == ["a.b"]


def test_case_only_merge_keeps_sibling_results(tmp_path: Path):
    store = ResultStore(None)
    store.merge([SuiteResult("a.b", "native_sim", "failed",
                             cases={"s.one": "passed", "s.two": "failed"})])
    # Re-running only s.two: twister reports s.one as skipped — must not clobber it.
    store.merge([SuiteResult("a.b", "native_sim", "passed",
                             cases={"s.one": "skipped", "s.two": "passed"})],
                only_cases={"s.two"})
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
    assert failures == {"demo_suite.fails": {
        "file": "CMAKE_SOURCE_DIR/src/main.c",
        "line": 12,
        "message": "(value not equal to 3) — value was 2",
    }}


def test_parse_ztest_failures_non_verbose_asserts():
    from lazyzephyr.twister.results import parse_ztest_failures

    log = "Running TESTSUITE s\nSTART - test_x\n\n    Assertion failed at /abs/t.c:7\n FAIL - test_x\n"
    assert parse_ztest_failures(log) == {"s.x": {"file": "/abs/t.c", "line": 7, "message": ""}}
    assert parse_ztest_failures(None) == {}


def test_case_only_merge_keeps_other_failures():
    store = ResultStore(None)
    store.merge([SuiteResult("a.b", "p", "failed", cases={"s.one": "failed", "s.two": "failed"},
                             failures={"s.one": {"line": 1}, "s.two": {"line": 2}})])
    store.merge([SuiteResult("a.b", "p", "passed", cases={"s.two": "passed"})],
                only_cases={"s.two"})
    assert store.for_scenario("a.b")["p"].failures == {"s.one": {"line": 1}}
