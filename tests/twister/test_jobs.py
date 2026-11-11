import asyncio
import json
from pathlib import Path

from lazyzephyr.twister.discovery import discover
from lazyzephyr.twister.jobs import JobManager, NodeRef, RunOptions, build_command, make_target


def refs(found, *names):
    by_name = found.by_name()
    return [NodeRef("scenario", by_name[n].directory, n) for n in names]


def test_folder_target_uses_plain_testsuite_root(test_tree: Path):
    found = discover([test_tree])
    target = make_target([NodeRef("folder", test_tree / "lib")], found)
    assert target.testsuite_roots == [test_tree / "lib"]
    assert target.scenarios == [] and target.test_filters == []
    assert target.covered == {"lib.alpha.default", "lib.alpha.variant"}


def test_scenarios_and_folder_mix_expands_folder(test_tree: Path):
    found = discover([test_tree])
    target = make_target(
        refs(found, "drivers.beta") + [NodeRef("folder", test_tree / "lib")], found
    )
    assert target.scenarios == ["drivers.beta", "lib.alpha.default", "lib.alpha.variant"]
    assert target.test_filters == []


def test_single_case_target(test_tree: Path):
    found = discover([test_tree])
    scenario = found.by_name()["lib.alpha.default"]
    case = scenario.cases[1]
    target = make_target([NodeRef("case", scenario.directory, scenario.name, case)], found)
    assert target.scenarios == ["lib.alpha.default"]
    assert target.test_filters == ["alpha_suite::test_with_fixture"]
    assert target.only_cases == {"alpha_suite.with_fixture"}


def test_case_plus_scenario_uses_suite_wildcards(test_tree: Path):
    found = discover([test_tree])
    alpha = found.by_name()["lib.alpha.default"]
    target = make_target(
        [NodeRef("case", alpha.directory, alpha.name, alpha.cases[0])] + refs(found, "drivers.beta"),
        found,
    )
    assert target.test_filters == ["alpha_suite::test_adds", "beta_suite::*"]
    assert target.only_cases is None


def test_build_command(test_tree: Path, tmp_path: Path):
    found = discover([test_tree])
    alpha = found.by_name()["lib.alpha.default"]
    target = make_target([NodeRef("case", alpha.directory, alpha.name, alpha.cases[0])], found)
    cmd = build_command(["west", "twister"], target,
                        RunOptions(platforms=["native_sim"], extra_args=["-v"]), tmp_path, 4)
    assert cmd == [
        "west", "twister", "-T", str(alpha.directory), "-s", "lib.alpha.default",
        "-p", "native_sim", "-G", "-i", "-v", "-j", "4", "-O", str(tmp_path), "--clobber-output",
        "-v",
        "--", "-test=alpha_suite::test_adds",
    ]


async def wait_until(predicate, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def test_queue_runs_one_job_at_a_time_in_order(test_tree, tmp_path, fake_twister,
                                                     monkeypatch):
    monkeypatch.setenv("FAKE_DELAY", "0.2")
    monkeypatch.setenv("FAKE_FAIL", "drivers.beta")
    found = discover([test_tree])
    finished, max_running = [], []
    manager = JobManager(fake_twister, tmp_path / "state", tmp_path,
                         on_finished=finished.append,
                         on_change=lambda job: max_running.append(len(manager.running())))
    options = RunOptions()
    a = manager.submit(make_target(refs(found, "lib.alpha.default"), found), options)
    b = manager.submit(make_target(refs(found, "drivers.beta"), found), options)
    c = manager.submit(make_target(refs(found, "lib.alpha.variant"), found), options)
    # Identical run already waiting -> not queued twice.
    assert manager.submit(make_target(refs(found, "lib.alpha.variant"), found), options) is None

    assert manager.current is a
    assert [j.state for j in (a, b, c)] == ["running", "queued", "queued"]
    assert manager.position(b) == 1 and manager.position(c) == 2

    await wait_until(lambda: len(finished) == 3)
    assert finished == [a, b, c]
    assert [j.state for j in (a, b, c)] == ["passed", "failed", "passed"]
    assert max(max_running) == 1
    assert a.outdir == b.outdir == c.outdir  # one reused output dir
    assert b.live_status == {"drivers.beta": "failed"}
    assert b.results[0].status == "failed"
    assert b.command[b.command.index("-s") + 1] == "drivers.beta"
    assert "-j" not in a.command  # twister's own default unless -j is given


async def test_cancel_and_reorder_queued_jobs(test_tree, tmp_path, fake_twister, monkeypatch):
    monkeypatch.setenv("FAKE_DELAY", "0.3")
    found = discover([test_tree])
    finished = []
    manager = JobManager(fake_twister, tmp_path / "state", tmp_path, on_finished=finished.append)
    options = RunOptions()
    a = manager.submit(make_target(refs(found, "lib.alpha.default"), found), options)
    b = manager.submit(make_target(refs(found, "drivers.beta"), found), options)
    c = manager.submit(make_target(refs(found, "lib.alpha.variant"), found), options)
    d = manager.submit(make_target([NodeRef("folder", test_tree)], found), options)

    manager.cancel(b)
    assert b.state == "cancelled" and manager.queued() == [c, d]
    assert manager.move(d, -1) and manager.queued() == [d, c]
    assert not manager.move(d, -1)  # already first
    assert not manager.move(a, 1)  # running jobs can't move

    await wait_until(lambda: len(finished) == 4)
    assert finished == [b, a, d, c]


async def test_next_job_waits_for_cancelled_process_to_exit(test_tree, tmp_path, fake_twister,
                                                            monkeypatch):
    monkeypatch.setenv("FAKE_DELAY", "10")
    found = discover([test_tree])
    finished = []
    manager = JobManager(fake_twister, tmp_path / "state", tmp_path, on_finished=finished.append)
    a = manager.submit(make_target(refs(found, "drivers.beta"), found), RunOptions())
    b = manager.submit(make_target(refs(found, "lib.alpha.default"), found), RunOptions())
    await asyncio.sleep(0.3)
    manager.cancel(a)
    assert a.state == "cancelled" and manager.current is a and b.state == "queued"
    await wait_until(lambda: a in finished)
    assert manager.current is b and b.state == "running"
    manager.cancel_all()
    await wait_until(lambda: b in finished)


async def test_cancel_running_job(test_tree, tmp_path, fake_twister, monkeypatch):
    monkeypatch.setenv("FAKE_DELAY", "10")
    found = discover([test_tree])
    finished = []
    manager = JobManager(fake_twister, tmp_path / "state", tmp_path, on_finished=finished.append)
    job = manager.submit(make_target(refs(found, "drivers.beta"), found), RunOptions())
    await asyncio.sleep(0.3)
    manager.cancel(job)
    for _ in range(100):
        if finished:
            break
        await asyncio.sleep(0.05)
    assert job.state == "cancelled"


async def test_missing_twister_reports_error(test_tree, tmp_path):
    found = discover([test_tree])
    finished = []
    manager = JobManager(["definitely-not-a-real-binary-xyz"], tmp_path, tmp_path,
                         on_finished=finished.append)
    job = manager.submit(make_target(refs(found, "drivers.beta"), found), RunOptions())
    await asyncio.sleep(0.2)
    assert job.state == "error"
    assert any("hint" in line for line in job.lines)


def test_parses_real_twister_lines():
    from lazyzephyr.twister.jobs import FAILURE_LINE_RE, RESULT_LINE_RE, ANSI_RE

    verbose = ("INFO    -  3/12 native_sim/native/64      libraries.encoding.json"
               "                            \x1b[32mPASSED\x1b[39m (native 0.004s <host>)")
    match = RESULT_LINE_RE.search(ANSI_RE.sub("", verbose))
    assert match and match.group(4) == "libraries.encoding.json" and match.group(5) == "PASSED"
    quiet = "ERROR   - native_sim/native/64      lib.x.native_sim   \x1b[31mFAILED\x1b[39m: Failed"
    match = FAILURE_LINE_RE.search(ANSI_RE.sub("", quiet))
    assert match and match.group(2) == "lib.x.native_sim"


def test_job_title_and_display_command(test_tree, tmp_path):
    from lazyzephyr.twister.jobs import display_command, job_title

    found = discover([test_tree])
    many = refs(found, "drivers.beta", "lib.alpha.default", "lib.alpha.variant")
    assert job_title(many[:1]) == "drivers.beta"
    assert job_title(many[:2]) == "drivers.beta, lib.alpha.default"
    assert job_title(many) == "drivers.beta, lib.alpha.default +1"
    assert make_target(many, found).title == "drivers.beta, lib.alpha.default +1"

    cmd = ["west", "twister", "-T", str(test_tree / "lib"), "-O", str(tmp_path / "out"),
           "--", "-test=a::b c"]
    assert display_command(cmd, test_tree.parent) == "west twister -T tests/lib -- '-test=a::b c'"


def test_no_clean_disables_clobber(tmp_path):
    from lazyzephyr.twister.jobs import RunTarget

    target = RunTarget("t", [tmp_path], [], [], set())
    assert "--clobber-output" in build_command(["tw"], target, RunOptions(), tmp_path, None)
    cmd = build_command(["tw"], target, RunOptions(extra_args=["-n"]), tmp_path, None)
    assert "--clobber-output" not in cmd
