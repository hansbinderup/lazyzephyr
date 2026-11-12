"""The combined app: both tools as tabs."""

import asyncio

from lazyzephyr.app import LazyZephyrApp
from lazyzephyr.builder.configs import BuildConfig
from lazyzephyr.dts.app import AppConfig as DtsConfig
from lazyzephyr.dts.state import TargetChoice
from lazyzephyr.dts.workspace import find_workspace
from lazyzephyr.twister.app import AppConfig as TwisterConfig
from lazyzephyr.twister.discovery import Workspace
from lazyzephyr.twister.jobs import RunOptions


async def wait_for(predicate, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


def twister_config(tmp_path, roots):
    return TwisterConfig(cwd=tmp_path, roots=roots, workspace=Workspace(None, None),
                         twister_cmd=["true"], state_dir=tmp_path / "state",
                         options=RunOptions())


async def test_tabs_switch_and_keep_their_own_keys(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    tests = tmp_path / "tests" / "t"
    tests.mkdir(parents=True)
    (tests / "testcase.yaml").write_text("tests:\n  demo.one: {}\n")
    dts = DtsConfig(cwd=workspace / "app", workspace=find_workspace(workspace),
                    choice=TargetChoice(board="native_sim/native", app=str(workspace / "app")),
                    work_dir=tmp_path / "work")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    app = LazyZephyrApp(twister_config(tmp_path, [tmp_path / "tests"]), dts)
    async with app.run_test(size=(180, 50)) as pilot:
        twister = app.twister
        await wait_for(lambda: "s:demo.one" in twister.node_index)
        # The other tabs load in the background without taking over the screen.
        dts_view = app.view("dts")
        await wait_for(lambda: dts_view.session is not None)
        await pilot.pause(0.3)
        assert app.tabs.active == "build"
        assert app.focused is not None and app.view("build") in app.focused.ancestors
        await pilot.press("right_square_bracket")
        await wait_for(lambda: app.tabs.active == "dts")
        view = app.view("dts")
        await wait_for(lambda: view.session is not None and "/leds/led_0" in view.node_index)
        await wait_for(lambda: app.focused is not None and view in app.focused.ancestors)
        # '/' opens the devicetree filter here, not the tests filter.
        await pilot.press("slash")
        assert app.focused is view.query_one("#filter")
        await pilot.press("escape")
        await pilot.press("f1")
        await wait_for(lambda: app.tabs.active == "build")
        build = app.view("build")
        await wait_for(lambda: app.focused is not None and build in app.focused.ancestors)
        await pilot.press("f4")
        await wait_for(lambda: app.tabs.active == "tests")
        await wait_for(lambda: app.focused is not None and twister in app.focused.ancestors)
        await pilot.press("q")
        await wait_for(lambda: not app.is_running)


def test_tab_order():
    from lazyzephyr.app import TABS
    assert TABS == ("build", "dts", "kconfig", "tests")


async def drag(pilot, x, y, dx, dy):
    await pilot.mouse_down(offset=(x, y))
    await pilot.hover(offset=(x + dx, y + dy))
    await pilot.mouse_up(offset=(x + dx, y + dy))
    await pilot.pause()


async def test_drag_and_maximize_in_every_tab(workspace, tmp_path):
    tests = tmp_path / "tests" / "t"
    tests.mkdir(parents=True)
    (tests / "testcase.yaml").write_text("tests:\n  demo.one: {}\n")
    dts = DtsConfig(cwd=workspace / "app", workspace=find_workspace(workspace),
                    choice=TargetChoice(board="native_sim/native", app=str(workspace / "app")),
                    work_dir=tmp_path / "work")
    app = LazyZephyrApp(twister_config(tmp_path, [tmp_path / "tests"]), dts)
    # (tab, left column, the two stacked panels on the right)
    tabs = [("build", "#b-left", "#b-configs-panel", "#b-details-panel"),
            ("dts", "#left", "#details-panel", "#settings-panel"),
            ("kconfig", "#kleft", "#kdetails-panel", "#krelated-panel"),
            ("tests", "#left", "#log", "#details-panel")]
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: "s:demo.one" in app.twister.node_index)
        for tab, left_id, upper_id, lower_id in tabs:
            app.tabs.active = tab
            await pilot.pause(0.3)
            if app.screen is not app.screen_stack[0]:   # Kconfig asks for a build
                await pilot.press("escape")
                await pilot.pause()
            view = app.view(tab)
            left = view.query_one(left_id)
            width = left.outer_size.width
            await drag(pilot, left.region.right - 1, left.region.y + 5, 10, 0)
            assert left.outer_size.width == width + 10, tab
            upper, lower = view.query_one(upper_id), view.query_one(lower_id)
            height = upper.outer_size.height
            await drag(pilot, upper.region.x + 5, upper.region.bottom - 1, 0, 3)
            assert upper.outer_size.height == height + 3, tab
            assert lower.region.y == upper.region.bottom, tab
            # z maximizes the focused panel itself, not the whole tab area.
            target = upper if upper.can_focus else next(
                w for w in upper.query("*") if w.can_focus)
            target.focus()
            await pilot.press("z")
            await pilot.pause()
            assert app.screen.maximized is upper, (tab, app.screen.maximized)
            await pilot.press("z")
            await pilot.pause()
            assert app.screen.maximized is None, tab
        # The monitor's ctrl+z, in the tabbed app.
        app.tabs.active = "build"
        await pilot.pause(0.3)
        build = app.view("build")
        build.store.configs.append(BuildConfig(name="mon", app="app", board="native_sim",
                                               monitor="echo ready; sleep 30"))
        build.refresh_list(select="cfg:mon")
        build.configs_list.focus()
        await pilot.press("o")
        await wait_for(lambda: build.monitor.process is not None and build.monitor.running)
        await pilot.press("ctrl+z")
        await pilot.pause()
        assert app.screen.maximized is build.query_one("#b-monitor-panel")
        await pilot.press("ctrl+z")
        await pilot.pause()
        assert app.screen.maximized is None
        await pilot.press("ctrl+x")
