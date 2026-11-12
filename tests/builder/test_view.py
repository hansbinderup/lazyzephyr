"""Drive the build tab headlessly, with a fake west."""

import asyncio
import sys
from pathlib import Path

from dataclasses import replace

from lazyzephyr.builder.app import FIELDS, BuildView, LazyBuildApp
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
        # New config through the settings list.
        await pilot.press("n")
        await pilot.pause()
        form = app.screen
        rows = [f.key for f in FIELDS]

        async def goto(key):
            form.query_one("#settings").highlighted = rows.index(key)
            await pilot.pause()

        async def type_value(key, text):
            await goto(key)
            await pilot.press("e")
            await pilot.pause()
            field = app.screen.query_one("#text-value")
            field.value = text
            await pilot.press("enter")
            await pilot.pause()

        # Moving around changes nothing.
        before = replace(form.draft)
        await pilot.press("down", "down", "j", "k", "up")
        assert form.draft == before
        # esc in an editor throws the change away.
        await goto("name")
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#text-value").value = "oops"
        await pilot.press("escape")
        await pilot.pause()
        assert form.draft.name == "" and app.screen is form
        await type_value("name", "sim")
        await type_value("app", "app")
        # enter on Board opens the searchable list.
        await goto("board")
        await pilot.press("enter")
        await pilot.pause()
        for ch in "native_sim/native/64":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()
        assert form.draft.board == "native_sim/native/64"
        await type_value("shields", "no_such_shield")
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert "unknown shield" in str(form.query_one("#f-error").render())
        await type_value("shields", "")
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


FAKE_DEVICE = ("echo \"hello from $BOARD\"; "
               "while read -r line; do echo \"got:$line\"; done")


def screen_text(view) -> str:
    return "\n".join(view.monitor.screen_.display)


async def test_monitor(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ws = find_workspace(workspace)
    store = ConfigStore(ws.topdir, tmp_path / "builds.json")
    store.configs.append(BuildConfig(name="sim", app="app", board="native_sim",
                                     monitor=FAKE_DEVICE))
    store.configs.append(BuildConfig(name="bare", app="app", board="native_sim"))
    store.save()
    view = BuildView(ws, workspace / "app", store=store, west=FAKE_WEST, id="builder")
    app = LazyBuildApp(view)
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: view.options is not None)
        view.configs_list.focus()
        await pilot.press("o")
        await wait_for(lambda: view.monitor.running)
        assert app.focused is view.monitor
        await wait_for(lambda: "hello from native_sim" in screen_text(view))
        # Keys go to the device, also ones the tab or app would otherwise take.
        for key in ("q", "b", "x", "enter"):
            await pilot.press(key)
        await wait_for(lambda: "got:qbx" in screen_text(view))
        assert app.is_running and view.job is None
        # Full screen and back.
        await pilot.press("f11")
        await pilot.pause()
        assert app.screen.maximized is view.query_one("#b-monitor-panel")
        await pilot.press("f11")
        await pilot.pause()
        assert app.screen.maximized is None
        # ctrl+o leaves the monitor running.
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert app.focused is view.configs_list and view.monitor.running
        proc = view.monitor.process.proc
        # O closes the connection.
        await pilot.press("O")
        await wait_for(lambda: proc.poll() is not None)
        assert view.monitor_config is None and not view.has_class("-monitor")
        # A config without a command asks for one, saves it and opens the monitor.
        await pilot.press("down")
        await pilot.press("o")
        await pilot.pause()
        app.screen.query_one("#text-value").value = "echo bare monitor; sleep 30"
        await pilot.press("enter")
        await wait_for(lambda: "bare monitor" in screen_text(view))
        assert ConfigStore(ws.topdir, tmp_path / "builds.json").configs[1].monitor \
            == "echo bare monitor; sleep 30"
        proc = view.monitor.process.proc
    await asyncio.sleep(0.2)
    assert proc.poll() is not None  # quitting the app closes the monitor


def test_key_bytes():
    from textual import events

    from lazyzephyr.builder.monitor import _color, key_bytes
    assert key_bytes(events.Key("ctrl+c", None)) == b"\x03"
    assert key_bytes(events.Key("up", None)) == b"\x1b[A"
    assert key_bytes(events.Key("up", None), app_cursor=True) == b"\x1bOA"
    assert key_bytes(events.Key("a", "a")) == b"a"
    assert key_bytes(events.Key("enter", "\r")) == b"\r"
    assert _color("brightbrown") == "bright_yellow" and _color("ff8800") == "#ff8800"
    assert _color("default") is None
