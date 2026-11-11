"""The combined app: both tools as tabs."""

import asyncio

from lazyzephyr.app import LazyZephyrApp
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
        assert app.tabs.active == "tests"
        await pilot.press("right_square_bracket")
        await wait_for(lambda: app.tabs.active == "dts")
        view = app.view("dts")
        await wait_for(lambda: view.session is not None and "/leds/led_0" in view.node_index)
        await wait_for(lambda: app.focused is not None and view in app.focused.ancestors)
        # '/' opens the devicetree filter here, not the tests filter.
        await pilot.press("slash")
        assert app.focused is view.query_one("#filter")
        await pilot.press("escape")
        await pilot.press("f3")
        await wait_for(lambda: app.tabs.active == "build")
        build = app.view("build")
        await wait_for(lambda: app.focused is not None and build in app.focused.ancestors)
        await pilot.press("f1")
        await wait_for(lambda: app.tabs.active == "tests")
        await wait_for(lambda: app.focused is not None and twister in app.focused.ancestors)
        await pilot.press("q")
        await wait_for(lambda: not app.is_running)
