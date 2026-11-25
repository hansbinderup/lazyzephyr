"""Drive the build tab headlessly, with a fake west."""

import asyncio
import sys
from dataclasses import replace
from pathlib import Path

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
        assert (
            ConfigStore(ws.topdir, tmp_path / "builds.json").configs[0].board
            == "native_sim/native/64"
        )
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
        await wait_for(
            lambda: (
                store.configs[0].last_action == "build_flash"
                and store.configs[0].last_status == "passed"
            )
        )
        assert "fake west flash -d" in "\n".join(view.last_job.lines)


FAKE_DEVICE = 'echo "hello from $BOARD"; while read -r line; do echo "got:$line"; done'


def screen_text(view) -> str:
    return "\n".join(view.monitor.screen_.display)


async def test_monitor(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ws = find_workspace(workspace)
    store = ConfigStore(ws.topdir, tmp_path / "builds.json")
    store.configs.append(
        BuildConfig(name="sim", app="app", board="native_sim", monitor=FAKE_DEVICE)
    )
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
        # The footer shows only the monitor's own keys: the rest go to the device.
        shown = {b.binding.key for b in app.screen.active_bindings.values() if b.binding.show}
        assert {"ctrl+o", "ctrl+z", "ctrl+x"} <= shown and not shown & {"x", "question_mark"}
        # Full screen and back.
        await pilot.press("ctrl+z")
        await pilot.pause()
        assert app.screen.maximized is view.query_one("#b-monitor-panel")
        await pilot.press("ctrl+z")
        await pilot.pause()
        assert app.screen.maximized is None
        await pilot.press("f11")
        await pilot.pause()
        assert app.screen.maximized is view.query_one("#b-monitor-panel")
        # ctrl+x closes it, also from full screen.
        proc = view.monitor.process.proc
        await pilot.press("ctrl+x")
        await wait_for(lambda: proc.poll() is not None)
        assert view.monitor_config is None and app.screen.maximized is None
        assert app.focused is view.configs_list
        # Open again for the rest.
        await pilot.press("o")
        await wait_for(lambda: view.monitor.process is not None and view.monitor.running)
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
        assert (
            ConfigStore(ws.topdir, tmp_path / "builds.json").configs[1].monitor
            == "echo bare monitor; sleep 30"
        )
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


def test_monitor_devices(tmp_path):
    import os
    import subprocess
    import time

    from lazyzephyr.builder.monitor import command_devices, session_devices

    link = tmp_path / "port"
    link.symlink_to("/dev/zero")
    assert command_devices("tio /dev/ttyACM0 -b 115200") == {"/dev/ttyACM0"}
    assert command_devices("west espressif monitor --port=/dev/ttyUSB1") == {"/dev/ttyUSB1"}
    assert command_devices("west espressif monitor -d $BUILD_DIR") == set()
    # The ports open in the session, even ones the command doesn't name.
    proc = subprocess.Popen(
        ["sh", "-c", 'exec 3<"$PORT"; exec sleep 30'],
        env=dict(os.environ, PORT=str(link)),
        start_new_session=True,
    )
    try:
        for _ in range(100):
            if "/dev/zero" in session_devices(proc.pid):
                break
            time.sleep(0.05)
        assert "/dev/zero" in session_devices(proc.pid)
    finally:
        proc.kill()
        proc.wait()


def footer_keys(app):
    return {key for key, b in app.screen.active_bindings.items() if b.binding.show and b.enabled}


async def test_monitor_select_and_copy():
    """Dragging selects monitor text; ctrl+c copies it instead of reaching the device."""
    from textual.app import App
    from textual.geometry import Offset
    from textual.selection import Selection

    from lazyzephyr.builder.monitor import MonitorTerminal

    class TermApp(App):
        def compose(self):
            yield MonitorTerminal()

    app = TermApp()
    async with app.run_test(size=(40, 4)) as pilot:
        term = app.query_one(MonitorTerminal)
        term.focus()
        sent = []

        class FakeProcess:
            running = True

            def write(self, data):
                sent.append(data)

            def close(self):
                pass

        term.process = FakeProcess()
        assert "ctrl+c" not in footer_keys(app)
        term._output(b"one\r\ntwo\r\nthree\r\nfour\r\nfive")
        await pilot.pause()
        scrolled = term.screen_.scrolled
        assert scrolled == 1  # "one" is in the history
        await pilot.mouse_down(term, offset=(1, 0))
        await pilot.hover(term, offset=(2, 2))
        await pilot.mouse_up(term, offset=(2, 2))
        await pilot.pause()
        assert app.screen.get_selected_text() == "wo\nthree\nfou"
        assert "ctrl+c" in footer_keys(app)  # the footer offers to copy it
        # The selection keeps the text readable: its own colour on the selection's background.
        cell = next(seg for seg in term.render_line(0) if seg.text.startswith("wo")).style
        assert cell.color != cell.bgcolor
        # More output scrolls the screen; the selection stays on the same text.
        term._output(b"\r\nsix")
        await pilot.pause()
        assert app.screen.get_selected_text() == "wo\nthree\nfou"
        # A selection reaching back into history.
        app.screen.selections = {
            term: Selection.from_offsets(Offset(0, scrolled - 1), Offset(3, scrolled))
        }
        assert app.screen.get_selected_text() == "one\ntwo"
        copied = []
        app.copy_to_clipboard = copied.append
        await pilot.press("ctrl+c")
        assert copied == ["one\ntwo"] and sent == []
        assert term.text_selection is None
        await pilot.pause()
        assert "ctrl+c" not in footer_keys(app)
        await pilot.press("ctrl+c")  # nothing selected: ctrl+c goes to the device
        assert sent == [b"\x03"]


def term_screen(columns, lines):
    import pyte

    from lazyzephyr.builder.monitor import TermScreen

    screen = TermScreen(columns, lines, lambda data: None)
    return screen, pyte.ByteStream(screen)


def history(screen):
    return [screen.line_text(-i) for i in range(len(screen.scrollback), 0, -1)]


def test_monitor_resize_keeps_text():
    """A shorter screen moves lines into the history, a taller one brings them back."""
    screen, stream = term_screen(20, 10)
    stream.feed(b"".join(b"line%d\r\n" % i for i in range(9)) + b"uart:~$ ")
    screen.resize(4, 20)
    assert history(screen) == [f"line{i}" for i in range(6)]
    assert [screen.line_text(r) for r in range(4)] == ["line6", "line7", "line8", "uart:~$"]
    assert screen.cursor.y == 3 and screen.scrolled == 6
    stream.feed(b"help")
    assert screen.line_text(3) == "uart:~$ help"  # typing still shows
    screen.resize(10, 20)
    assert screen.line_text(0) == "line0" and screen.line_text(9) == "uart:~$ help"
    assert history(screen) == [] and screen.scrolled == 0
    # A few lines fit the shorter screen: none of them leave it.
    screen, stream = term_screen(20, 10)
    stream.feed(b"one\r\ntwo")
    screen.resize(4, 20)
    assert [screen.line_text(r) for r in range(2)] == ["one", "two"] and history(screen) == []


def test_monitor_clear_keeps_history():
    """Clearing the screen (or resetting the terminal) keeps its text in the history."""
    screen, stream = term_screen(20, 5)
    stream.feed(b"boot\r\nuart:~$ clear\r\n\x1b[H\x1b[2Juart:~$ ")
    assert history(screen) == ["boot", "uart:~$ clear"] and screen.line_text(0) == "uart:~$"
    stream.feed(b"\x1bc")
    assert history(screen) == ["boot", "uart:~$ clear", "uart:~$"]
    stream.feed(b"\x1b[2J")  # an empty screen adds nothing
    assert len(screen.scrollback) == 3


async def test_monitor_drawing():
    """Lines redraw when they change, wide characters take two cells, and the
    history view stays still while output scrolls."""
    from textual.app import App

    from lazyzephyr.builder import monitor

    class TermApp(App):
        def compose(self):
            yield monitor.MonitorTerminal()

    def text(term, y):
        return "".join(seg.text for seg in term.render_line(y)).rstrip()

    app = TermApp()
    async with app.run_test(size=(20, 3)) as pilot:
        term = app.query_one(monitor.MonitorTerminal)
        await pilot.pause()
        term._output(b"count 1\r\nnext")
        assert text(term, 0) == "count 1"
        term._output(b"\x1b[A\rcount 2")  # rewritten in place
        assert text(term, 0) == "count 2"
        term._output("\r\n中文 ok".encode())  # over "next"
        assert text(term, 1) == "中文 ok"
        assert sum(seg.cell_length for seg in term.render_line(1)) == 20
        # Reading the history, with a full history: the view doesn't move.
        term.screen_.scrollback = monitor.deque(maxlen=2)
        term._output(b"\r\na\r\nb\r\nc\r\nd")
        term._scroll_by(1)
        before = [text(term, y) for y in range(3)]
        assert term.scroll == 1 and before == ["a", "b", "c"]
        term._output(b"\r\ne")
        assert [text(term, y) for y in range(3)] == before


async def test_monitor_sends_a_long_paste(tmp_path):
    """What the terminal can't take at once is sent as it drains, not dropped."""
    import os

    from lazyzephyr.builder.monitor import MonitorProcess

    out = tmp_path / "out"
    data = bytes(range(ord("a"), ord("z") + 1)) * 8000  # 208 kB, far over the pty buffer
    ready, exited = asyncio.Event(), asyncio.Event()
    proc = MonitorProcess(
        f"stty raw -echo; echo ready; head -c {len(data)} > {out}",
        tmp_path,
        dict(os.environ),
        80,
        24,
        lambda chunk: b"ready" in chunk and ready.set(),
        lambda code: exited.set(),
    )
    await asyncio.wait_for(ready.wait(), 15)  # raw: no line length limit
    proc.write(data)
    await asyncio.wait_for(exited.wait(), 15)
    assert out.read_bytes() == data


async def test_missing_west_gets_a_banner(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr("lazyzephyr.builder.app.west_command", lambda topdir: None)
    ws = find_workspace(workspace)
    store = ConfigStore(ws.topdir, tmp_path / "builds.json")
    view = BuildView(ws, workspace / "app", store=store, id="builder")
    async with LazyBuildApp(view).run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: view.options is not None)
        banner = view.query_one("#b-no-west")
        assert view.has_class("-no-west") and banner.display
        assert "west not found" in str(banner.render())
        # Once west turns up (set up in the SDK tab, say), the banner goes.
        view.west = FAKE_WEST
        view.activated()
        await pilot.pause()
        assert not view.has_class("-no-west") and not banner.display
