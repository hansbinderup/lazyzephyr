"""Drive the build tab headlessly, with a fake west."""

import asyncio
import sys
from pathlib import Path

from lazyzephyr.builder.app import BuildView, LazyBuildApp
from lazyzephyr.builder.configs import BuildConfig, ConfigStore
from lazyzephyr.builder.runner import Job, Runner
from lazyzephyr.dts.workspace import find_workspace

FAKE_WEST = [sys.executable, str(Path(__file__).parent / "fake_west.py")]


async def wait_for(predicate, timeout=15.0):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def test_runner_chains_steps_and_stops_on_failure(tmp_path):
    lines = []
    r = Runner(tmp_path, lambda job, line: lines.append(line), lambda job: None, FAKE_WEST)
    ok = await r.run(Job("t", "c", "build_flash", [["build"], ["flash"]]))
    assert ok.status == "passed" and "fake west flash" in lines
    lines.clear()
    bad = await r.run(Job("t", "c", "build_flash", [["build", "FAKE_WEST_FAIL"], ["flash"]]))
    assert bad.status == "failed" and "fake west flash" not in lines


async def test_runner_cancel(tmp_path):
    r = Runner(tmp_path, lambda job, line: None, lambda job: None, FAKE_WEST)
    task = asyncio.ensure_future(r.run(Job("t", "c", "run", [["build", "-t", "run"]])))
    await wait_for(lambda: r._proc is not None)
    await asyncio.sleep(0.3)
    assert r.cancel()
    job = await task
    assert job.status == "cancelled"


async def test_build_tab(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ws = find_workspace(workspace)
    store = ConfigStore(ws.topdir, tmp_path / "builds.json")
    view = BuildView(ws, workspace / "app", store=store, west=FAKE_WEST, id="builder")
    app = LazyBuildApp(view)
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: view.options is not None)
        # New config through the form.
        await pilot.press("n")
        await pilot.pause()
        form = app.screen
        form.query_one("#f-name").value = "sim"
        form.query_one("#f-app").value = "app"
        form.query_one("#f-board").value = "native_sim/native/64"
        await pilot.pause()
        form.query_one("#f-shields").value = "no_such_shield"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "unknown shield" in str(form.query_one("#f-error").render())
        form.query_one("#f-shields").value = ""
        await pilot.press("ctrl+s")
        await wait_for(lambda: [c.name for c in store.configs] == ["sim"])
        assert ConfigStore(ws.topdir, tmp_path / "builds.json").configs[0].board == \
            "native_sim/native/64"
        # Build: the fake west gets the right arguments.
        view.configs_list.focus()
        await pilot.press("b")
        await wait_for(lambda: store.configs[0].last_status == "passed")
        log = "\n".join(view.last_job.lines)
        assert "fake west build -d" in log and "-b native_sim/native/64" in log
        # Flash needs a build dir first.
        await pilot.press("f")
        await pilot.pause()
        assert store.configs[0].last_action == "build"
        # Action menu: pick "Build, then flash".
        d = Path(view.ws.topdir) / "build-app-native_sim_native_64"
        d.mkdir()
        (d / "CMakeCache.txt").write_text("")
        await pilot.press("enter")
        await pilot.pause()
        for ch in "then flash":
            await pilot.press(ch if ch != " " else "space")
        await pilot.press("enter")
        await wait_for(lambda: store.configs[0].last_action == "build_flash"
                       and store.configs[0].last_status == "passed")
        assert "fake west flash -d" in "\n".join(view.last_job.lines)
