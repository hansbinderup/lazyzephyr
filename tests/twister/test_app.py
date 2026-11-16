"""Smoke tests driving the TUI headlessly with Textual's pilot."""

import asyncio
from pathlib import Path

from rich.text import Text

from lazyzephyr.twister.app import (
    AppConfig,
    LazyTwisterApp,
    TwisterView,
    count_text,
    fit_status,
    short_port,
)
from lazyzephyr.twister.discovery import Workspace
from lazyzephyr.twister.jobs import Hardware, RunOptions


def make_app(test_tree: Path, tmp_path: Path, fake_twister) -> LazyTwisterApp:
    return LazyTwisterApp(
        AppConfig(
            cwd=tmp_path,
            roots=[test_tree],
            workspace=Workspace(None, None),
            twister_cmd=fake_twister,
            state_dir=tmp_path / "state",
            options=RunOptions(),
        )
    )


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
    test_tree, tmp_path, fake_twister, monkeypatch
):
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
    assert pick(160, 90) == ("portrait", False)  # tall window
    assert pick(100, 60) == ("portrait", False)  # narrow split
    assert pick(80, 30) == ("portrait", True)  # small
    assert pick(200, 20) == ("landscape", True)  # short
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


def test_fit_status_drops_low_priority_parts_first():
    left = [(1, Text("workspace")), (3, Text("native_sim")), (2, Text("tests"))]
    right = Text("3 scenarios")
    assert str(fit_status(left, right)) == "workspace · native_sim · tests · 3 scenarios"
    wide = fit_status(left, right, 60)
    assert len(wide) == 60 and str(wide).endswith("  3 scenarios")
    assert str(fit_status(left, right, 40)).startswith("native_sim · tests  ")
    assert str(fit_status(left, right, 25)).startswith("native_sim  ")
    assert str(fit_status(left, right, 14)).strip() == "3 scenarios"
    assert str(fit_status(left, right, 8)) == "3 scena…"


def test_short_port():
    assert short_port("/dev/serial/by-id/usb-SEGGER_J-Link_000683-if00") == "SEGGER_J-Link_000683"
    assert short_port("/dev/serial/by-id/usb-FTDI_FT232R_A1-if00-port0") == "FTDI_FT232R_A1"
    assert short_port("/dev/ttyACM0") == "/dev/ttyACM0"


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
    monkeypatch.setattr(
        "lazyzephyr.twister.app.subprocess.run", lambda cmd, check: opened.append(cmd)
    )
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
    tmp_path, fake_twister, monkeypatch
):
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


async def test_platform_only_filter(test_tree, tmp_path, fake_twister):
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        await pilot.press("P")  # no platform chosen yet: refused
        assert not app.view.platform_only and "s:drivers.beta" in app.node_index

        app.options.platforms = ["qemu_x86"]
        await pilot.press("P")
        await wait_for(lambda: "s:drivers.beta" not in app.node_index)
        assert "s:lib.alpha.variant" in app.node_index
        assert "s:lib.alpha.default" not in app.node_index
        assert "qemu_x86" in str(app.query_one("#tests-panel").border_title)

        await pilot.press("P")
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        assert str(app.query_one("#tests-panel").border_title) == "[1] Tests"


async def test_cancelled_job_is_listed_once_until_it_exits(
    test_tree, tmp_path, fake_twister, monkeypatch
):
    monkeypatch.setenv("FAKE_DELAY", "3")
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        app.tree.move_cursor(app.node_index["s:drivers.beta"])
        await pilot.press("enter")
        await wait_for(
            lambda: app.jobs.current is not None and app.jobs.current.process is not None
        )
        job = app.jobs.current
        app.jobs.cancel(job)
        # Cancelled, but still the current job until twister exits.
        assert job.state == "cancelled" and app.jobs.current is job
        assert app.job_order() == [job]
        app.view.refresh_jobs()  # used to raise DuplicateID
        await wait_for(lambda: app.jobs.current is None, timeout=10)
        assert app.job_order() == [job]


async def test_settings_are_saved_for_next_time(test_tree, tmp_path, fake_twister):
    import json

    from lazyzephyr.twister import session

    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        await pilot.press("V")
        app.options.platforms = ["qemu_x86"]
        await pilot.press("P")
        await pilot.pause()
    saved = json.loads((tmp_path / "state" / "session.json").read_text())
    assert saved["case_results"] is True and saved["platform_only"] is True
    assert saved["platforms"] == ["qemu_x86"]
    assert session.load(tmp_path / "state").case_results is True


async def test_hardware_setup_is_per_platform(test_tree, tmp_path, fake_twister):
    import json

    from lazyzephyr.builder.app import PickerScreen
    from lazyzephyr.twister.hardware import FixtureScreen, HardwareScreen

    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        await pilot.press("d")  # no platform chosen: nothing to set up
        await pilot.pause()
        assert not isinstance(app.screen, HardwareScreen)

        app.options.platforms = ["frdm_k64f"]
        await pilot.press("d")
        await wait_for(lambda: isinstance(app.screen, HardwareScreen))
        assert app.screen.platform == "frdm_k64f" and not app.screen.simulated
        await pilot.press("enter")  # "Run on the board": on
        await pilot.press("down", "enter")  # serial port: picker, then "other…"
        await pilot.press(*"other", "enter")
        await pilot.press(*"/dev/ttyFAKE", "enter")
        await pilot.press("down", "down", "down", "down", "enter")  # fixtures
        await wait_for(lambda: isinstance(app.screen, FixtureScreen))
        assert app.screen.known == {"gpio_loopback": 1}  # from drivers.beta's testcase.yaml
        await pilot.press("space", "enter")
        await wait_for(
            lambda: (
                isinstance(app.screen, HardwareScreen)
                and app.screen.draft.fixtures == ["gpio_loopback"]
            )
        )
        await pilot.press("ctrl+s")
        hw = app.options.boards["frdm_k64f"]
        assert (hw.enabled, hw.serial, hw.fixtures) == (True, "/dev/ttyFAKE", ["gpio_loopback"])
        assert "board: /dev/ttyFAKE" in str(app.view._status_line())
        saved = json.loads((tmp_path / "state" / "hardware.json").read_text())
        assert saved["frdm_k64f"]["serial"] == "/dev/ttyFAKE"

        # Another platform has its own (empty) setup; native_sim needs no board.
        app.options.platforms = ["native_sim"]
        app.view.update_status()
        assert "board:" not in str(app.view._status_line())
        await pilot.press("d")
        await wait_for(lambda: isinstance(app.screen, HardwareScreen))
        assert app.screen.simulated and [k for k, _ in app.screen.rows] == ["fixtures"]
        assert app.screen.draft.serial is None
        await pilot.press("escape")

        # A board and a simulated platform: the board runs alone, the rest without it.
        app.options.platforms = ["frdm_k64f", "native_sim"]
        app.tree.focus()
        app.tree.move_cursor(app.node_index["s:drivers.beta"])
        await pilot.press("V")  # case results in Output
        await pilot.press("enter")
        await wait_for(lambda: len(app.jobs.jobs) == 2)
        assert [j.options.platforms for j in app.jobs.jobs] == [["frdm_k64f"], ["native_sim"]]
        board_job = app.jobs.jobs[0]
        await wait_for(lambda: not board_job.active, timeout=10)
        assert board_job.state == "passed", board_job.lines
        assert any("── results" in line for line in board_job.lines)
        await wait_for(lambda: not app.jobs.jobs[1].active, timeout=10)

        # Two boards enabled: asked which one to run on.
        app.options.boards["nucleo_f429zi"] = Hardware(enabled=True, serial="/dev/ttyOTHER")
        app.options.platforms = ["frdm_k64f", "nucleo_f429zi"]
        app.view.update_status()
        assert "boards: frdm_k64f, nucleo_f429zi" in str(app.view._status_line())
        await pilot.press("enter")
        await wait_for(lambda: isinstance(app.screen, PickerScreen))
        await pilot.press(*"nucleo", "enter")
        await wait_for(lambda: len(app.jobs.jobs) == 3)
        assert app.jobs.jobs[2].options.platforms == ["nucleo_f429zi"]
        await wait_for(lambda: not app.jobs.jobs[2].active, timeout=10)
        argv = json.loads((tmp_path / "state" / "run" / "argv.json").read_text())
        assert argv[argv.index("--device-testing") : argv.index("-O")] == [
            "--device-testing",
            "--device-serial",
            "/dev/ttyOTHER",
        ]

        # Repeat reuses the board picked last instead of asking again.
        await pilot.press("full_stop")
        await wait_for(lambda: len(app.jobs.jobs) == 4)
        assert not isinstance(app.screen, PickerScreen)
        assert app.jobs.jobs[3].options.platforms == ["nucleo_f429zi"]


async def test_details_show_each_case_output(test_tree, tmp_path, fake_twister, monkeypatch):
    monkeypatch.setenv("FAKE_HANDLER_LOG", "1")
    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        app.tree.move_cursor(app.node_index["s:drivers.beta"])
        await pilot.press("enter")
        await wait_for(lambda: app.store.scenario_status("drivers.beta") == "passed")
        app.update_details()
        details = str(app.query_one("#details").render())
        assert "✔ suite.case_a" in details
        assert "│ hello from drivers.beta" in details
        # Survives a restart: kept in results.json.
        from lazyzephyr.twister.results import ResultStore

        stored = ResultStore(tmp_path / "state" / "results.json").for_scenario("drivers.beta")
        assert "hello from drivers.beta" in stored["native_sim"].case_logs["suite.case_a"]


async def test_case_details_show_full_output(test_tree, tmp_path, fake_twister):
    from lazyzephyr.twister.results import SuiteResult

    app = make_app(test_tree, tmp_path, fake_twister)
    async with app.run_test(size=(160, 50)):
        await wait_for(lambda: "s:lib.alpha.default" in app.node_index)
        log = "\n".join(
            [
                "START - test_adds",
                *(f"line {i}" for i in range(40)),
                " PASS - test_adds in 0.001 seconds",
            ]
        )
        app.store.merge(
            [
                SuiteResult(
                    "lib.alpha.default",
                    "native_sim",
                    "passed",
                    cases={"alpha_suite.adds": "passed"},
                    case_logs={"alpha_suite.adds": log},
                )
            ]
        )
        app.node_index["s:lib.alpha.default"].expand()
        await wait_for(lambda: "c:lib.alpha.default:alpha_suite.adds" in app.node_index)

        app.tree.move_cursor(app.node_index["s:lib.alpha.default"])
        app.update_details()
        scenario = str(app.query_one("#details").render())
        assert "… 27 earlier lines" in scenario and "line 39" in scenario
        assert "line 0\n" not in scenario

        app.tree.move_cursor(app.node_index["c:lib.alpha.default:alpha_suite.adds"])
        app.update_details()
        case = str(app.query_one("#details").render())
        assert "output · native_sim" in case and "line 0\n" in case and "line 39" in case
