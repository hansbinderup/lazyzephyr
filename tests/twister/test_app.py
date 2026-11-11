"""Smoke tests driving the TUI headlessly with Textual's pilot."""

import asyncio
from pathlib import Path

from lazyzephyr.twister.app import AppConfig, LazyTwisterApp, TwisterView, count_text
from lazyzephyr.twister.discovery import Workspace
from lazyzephyr.twister.jobs import RunOptions


def make_app(test_tree: Path, tmp_path: Path, fake_twister) -> LazyTwisterApp:
    return LazyTwisterApp(AppConfig(
        cwd=tmp_path,
        roots=[test_tree],
        workspace=Workspace(None, None),
        twister_cmd=fake_twister,
        state_dir=tmp_path / "state",
        options=RunOptions(),
    ))


async def wait_for(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def test_tree_shows_scenarios_and_runs_one(test_tree, tmp_path, fake_twister):
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        # Cases are only created once their scenario is expanded.
        assert "c:lib.alpha.default:alpha_suite.adds" not in app.node_index
        app.node_index["s:lib.alpha.default"].expand()
        await wait_for(lambda: "c:lib.alpha.default:alpha_suite.adds" in app.node_index)
        app.tree.move_cursor(app.node_index["s:drivers.beta"])
        await pilot.press("enter")
        await wait_for(lambda: app.store.scenario_status("drivers.beta") == "passed")
        assert app.jobs.jobs[0].state == "passed"


async def test_marked_nodes_queue_as_one_job_and_enter_queues_more(
        test_tree, tmp_path, fake_twister, monkeypatch):
    monkeypatch.setenv("FAKE_DELAY", "1.0")
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        app.tree.move_cursor(app.node_index["s:drivers.beta"])
        await pilot.press("space")
        app.tree.move_cursor(app.node_index["s:lib.alpha.default"])
        await pilot.press("space")
        await pilot.press("r")
        assert len(app.jobs.jobs) == 1 and not app.marks
        assert app.jobs.jobs[0].target.scenarios == ["drivers.beta", "lib.alpha.default"]

        app.tree.move_cursor(app.node_index["s:lib.alpha.variant"])
        await pilot.press("enter")
        app.tree.move_cursor(app.node_index["s:drivers.beta"])
        await pilot.press("enter")
        assert len(app.jobs.queued()) == 2

        # Jobs panel: running first, then queue order. Cancel the last queued job.
        await pilot.press("3")
        order = app.job_order()
        assert order[0] is app.jobs.current
        # As a real highlight does (via OptionHighlighted); otherwise a refresh tick
        # in between restores the highlight to the job whose output is shown.
        app.viewed_job = order[2].id
        app.query_one("#jobs").highlighted = 2
        await pilot.press("x")
        assert order[2].state == "cancelled" and len(app.jobs.queued()) == 1

        await wait_for(lambda: all(not j.active for j in app.jobs.jobs), timeout=15)
        assert [j.state for j in app.jobs.jobs] == ["passed", "passed", "cancelled"]


async def test_filter_narrows_tree(test_tree, tmp_path, fake_twister):
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        await pilot.press("slash", *"beta")
        await wait_for(lambda: "s:lib.alpha.default" not in app.node_index)
        assert "s:drivers.beta" in app.node_index
        await pilot.press("escape")
        await pilot.pause()
        assert "s:lib.alpha.default" in app.node_index


async def test_new_testcase_is_picked_up_live(test_tree, tmp_path, fake_twister):
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)):
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        await wait_for(lambda: app.watching)  # the watcher runs in a child process
        new = test_tree / "subsys" / "gamma" / "testcase.yaml"
        new.parent.mkdir(parents=True)
        new.write_text("tests:\n  subsys.gamma: {}\n")
        await wait_for(lambda: "s:subsys.gamma" in app.node_index, timeout=10)


def test_pick_layout():
    pick = TwisterView.pick_layout
    assert pick(200, 50) == ("landscape", False)
    assert pick(160, 90) == ("portrait", False)   # tall window
    assert pick(100, 60) == ("portrait", False)   # narrow split
    assert pick(80, 30) == ("portrait", True)     # small
    assert pick(200, 20) == ("landscape", True)   # short
    assert pick(80, 30, "landscape") == ("landscape", True)


async def test_layout_adapts_to_resize(test_tree, tmp_path, fake_twister):
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(200, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        screen = app.main_screen
        assert not screen.has_class("-portrait") and not screen.has_class("-compact")
        left, right = app.query_one("#left"), app.query_one("#right")
        assert left.region.y == right.region.y  # side by side

        await pilot.resize_terminal(100, 70)
        await pilot.pause()
        assert screen.has_class("-portrait") and not screen.has_class("-compact")
        assert left.region.x == right.region.x and right.region.y > left.region.y  # stacked

        await pilot.resize_terminal(80, 30)
        await pilot.pause()
        assert screen.has_class("-compact")
        assert not app.query_one("#details-panel").display
        await pilot.press("5")
        assert type(app.screen).__name__ == "InfoScreen"
        await pilot.press("escape")
        assert type(app.screen).__name__ != "InfoScreen"

        await pilot.press("v")  # auto -> forced landscape
        await pilot.pause()
        assert app.layout_mode == "landscape" and not screen.has_class("-portrait")


async def test_zoom_maximizes_focused_panel(test_tree, tmp_path, fake_twister):
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(200, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        await pilot.press("z")
        assert app.screen.maximized is app.query_one("#tests-panel")
        await pilot.press("z")
        assert app.screen.maximized is None
        await pilot.press("4", "z")
        assert app.screen.maximized is app.query_one("#log")


async def test_status_bar_at_bottom_and_full_status_popup(test_tree, tmp_path, fake_twister):
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(200, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        bar = app.query_one("#statusbar")
        assert bar.region.height == 1
        assert bar.region.y == 50 - 2  # just above the footer
        assert "3 scenarios" in str(bar.render())
        tests_panel = app.query_one("#tests-panel")
        assert tests_panel.region.bottom == bar.region.y  # tree uses the full left column
        await pilot.press("2")
        assert type(app.screen).__name__ == "InfoScreen"


async def test_quit_asks_while_jobs_are_running(test_tree, tmp_path, fake_twister, monkeypatch):
    monkeypatch.setenv("FAKE_DELAY", "10")
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        app.tree.move_cursor(app.node_index["s:drivers.beta"])
        await pilot.press("enter")
        await pilot.press("q")
        assert type(app.screen).__name__ == "ConfirmScreen"
        await pilot.press("n")
        assert app.is_running and app.jobs.current is not None
        await pilot.press("q", "y")
        await pilot.pause()
        assert not app.is_running


async def test_repeat_last_run(test_tree, tmp_path, fake_twister):
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        await pilot.press("full_stop")  # nothing yet: no job
        assert not app.jobs.jobs
        app.tree.move_cursor(app.node_index["s:drivers.beta"])
        await pilot.press("enter")
        await wait_for(lambda: not app.jobs.jobs[0].active)
        await pilot.press("full_stop")
        assert len(app.jobs.jobs) == 2
        assert app.jobs.jobs[1].target.signature() == app.jobs.jobs[0].target.signature()


async def test_edit_jumps_to_failing_assert(test_tree, tmp_path, fake_twister, monkeypatch):
    import contextlib

    log = tmp_path / "fail.log"
    log.write_text(
        "Running TESTSUITE beta_suite\nSTART - test_one\n\n"
        "    Assertion failed at CMAKE_SOURCE_DIR/src/test.c:1: beta_suite_test_one: "
        "(1 not equal to 2)\nextra detail\n FAIL - test_one in 0.0 seconds\n"
    )
    monkeypatch.setenv("FAKE_FAIL", "drivers.beta")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("EDITOR", "my-editor")
    opened = []
    monkeypatch.setattr("lazyzephyr.twister.app.subprocess.run",
                        lambda cmd, check: opened.append(cmd))
    app = make_app(test_tree, tmp_path, fake_twister)
    monkeypatch.setattr(app, "suspend", contextlib.nullcontext)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        app.tree.move_cursor(app.node_index["s:drivers.beta"])
        await pilot.press("enter")
        await wait_for(lambda: app.store.scenario_status("drivers.beta") == "failed")
        source = test_tree / "drivers" / "beta" / "src" / "test.c"

        details = str(app.query_one("#details").render())
        assert "src/test.c:1" in details and "1 not equal to 2" in details

        app.node_index["s:drivers.beta"].expand()
        await wait_for(lambda: "c:drivers.beta:beta_suite.one" in app.node_index)
        app.tree.move_cursor(app.node_index["c:drivers.beta:beta_suite.one"])
        await pilot.pause()
        assert "failed at" in str(app.query_one("#details").render())
        await pilot.press("e")
        assert opened == [["my-editor", "+1", str(source)]]


async def test_clear_cache_from_the_app(test_tree, tmp_path, fake_twister):
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        app.tree.move_cursor(app.node_index["s:drivers.beta"])
        await pilot.press("enter")
        await wait_for(lambda: app.store.scenario_status("drivers.beta") == "passed")
        state = tmp_path / "state"
        assert (state / "run").is_dir() and (state / "results.json").is_file()

        await pilot.press("C")
        await wait_for(lambda: type(app.screen).__name__ == "CacheScreen")
        await pilot.press("escape")
        assert (state / "run").is_dir()

        await pilot.press("C")
        await wait_for(lambda: type(app.screen).__name__ == "CacheScreen")
        await pilot.press("b")
        await wait_for(lambda: not (state / "run").exists())
        assert app.store.scenario_status("drivers.beta") == "passed"  # results kept

        await pilot.press("C")
        await wait_for(lambda: type(app.screen).__name__ == "CacheScreen")
        await pilot.press("a")
        await wait_for(lambda: not (state / "results.json").exists())
        assert app.store.scenario_status("drivers.beta") is None


def test_count_text_splits_passed_failed_and_not_run():
    text = count_text(["passed", "passed", "filtered", "failed", "error", None, "skipped"])
    assert text.plain == "2/2/3"
    number_styles = [span.style for span in text.spans if span.style != "bright_black"]
    assert number_styles == ["green", "red", "blue"]


async def test_big_tree_opens_only_roots_and_builds_children_on_expand(
        tmp_path, fake_twister, monkeypatch):
    root = tmp_path / "tests"
    for group in range(4):
        for n in range(3):
            directory = root / f"group{group}" / f"t{n}"
            directory.mkdir(parents=True)
            (directory / "testcase.yaml").write_text(f"tests:\n  g{group}.t{n}: {{}}\n")
    monkeypatch.setattr(TwisterView, "OPEN_ALL_MAX", 5)  # 12 scenarios = "big"
    app = make_app(root, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: f"d:{root / 'group0'}" in app.node_index)
        assert "s:g0.t0" not in app.node_index  # group folders start closed
        assert "4 scenarios" not in str(app.query_one("#statusbar").render())
        group0 = app.node_index[f"d:{root / 'group0'}"]
        app.tree.move_cursor(group0)
        await pilot.press("l")
        await wait_for(lambda: f"d:{root / 'group0' / 't0'}" in app.node_index)
        app.node_index[f"d:{root / 'group0' / 't0'}"].expand()
        await wait_for(lambda: "s:g0.t0" in app.node_index)

        # Stays open across a rebuild; a filter opens everything it matches.
        await app.rediscover()
        assert "s:g0.t0" in app.node_index and "s:g1.t0" not in app.node_index
        await pilot.press("slash", *"g2.t1")
        await wait_for(lambda: "s:g2.t1" in app.node_index)

