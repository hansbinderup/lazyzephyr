"""Drive the TUI headlessly against a throwaway workspace."""

import asyncio

from textual.widgets import Input

from lazyzephyr.dts.app import AppConfig, LazyDtsApp
from lazyzephyr.dts.editors import SpecifierEditor
from lazyzephyr.dts.state import TargetChoice
from lazyzephyr.dts.workspace import find_workspace


async def wait_for(predicate, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def test_browse_toggle_and_add(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ws = find_workspace(workspace)
    app = LazyDtsApp(AppConfig(cwd=workspace / "app", workspace=ws,
                               choice=TargetChoice(board="native_sim/native",
                                                   app=str(workspace / "app")),
                               work_dir=tmp_path / "work"))
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: "/leds/led_0" in app.node_index)
        await wait_for(lambda: app.bindings_catalog)
        i2c = next(p for p in app.node_index if p.startswith("/i2c@"))
        app.tree.move_cursor(app.node_index[i2c])
        await pilot.pause()
        await pilot.press("s")
        await wait_for(lambda: 'status = "disabled";' in overlay.read_text())
        await pilot.press("s")
        await wait_for(lambda: 'status = "okay";' in overlay.read_text())
        # Add the module's widget under the I2C bus.
        app.tree.move_cursor(app.node_index[i2c])
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        for ch in "vnd,widget":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*"0x10")
        form = app.screen
        form.query_one("#prop-mode").value = "fast"
        await pilot.press("ctrl+s")
        await wait_for(lambda: "widget: widget@10 {" in overlay.read_text())
        text = overlay.read_text()
        assert 'compatible = "vnd,widget";' in text and 'mode = "fast";' in text
        await wait_for(lambda: f"{i2c}/widget@10" in app.node_index)
        # Undo restores the file.
        await pilot.press("u")
        await wait_for(lambda: "widget" not in overlay.read_text())


async def test_edit_property_dialog(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ws = find_workspace(workspace)
    app = LazyDtsApp(AppConfig(cwd=workspace / "app", workspace=ws,
                               choice=TargetChoice(board="native_sim/native",
                                                   app=str(workspace / "app")),
                               work_dir=tmp_path / "work"))
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: "/leds/led_0" in app.node_index)
        app.node_index["/leds"].expand()
        await pilot.pause()
        app.tree.move_cursor(app.node_index["/leds/led_0"])
        props = app.query_one("#props")
        await wait_for(lambda: props.option_count and app.current_path == "/leds/led_0")
        props.focus()
        ids = [props.get_option_at_index(i).id for i in range(props.option_count)]
        props.highlighted = ids.index("gpios")
        await pilot.press("enter")
        await pilot.pause()
        # gpios is edited as rows of controller + named cells.
        screen = app.screen
        editor = screen.query_one("#edit-value", SpecifierEditor)
        assert editor.entries() == [("&gpio0", ["0", "0"])]
        pin, flags = editor.query(".spec-cell").results(Input)
        assert pin.placeholder.startswith("pin") and flags.placeholder == "flags"
        pin.value = "3"
        # Flags offer the dt-bindings GPIO macros; down + enter puts one in.
        flags.focus()
        flags.value = "GPIO_ACTIVE_L"
        flags.cursor_position = len(flags.value)
        await pilot.pause()
        assert screen.completer.display and screen.completer.target is flags
        await pilot.press("down", "enter")
        assert flags.value == "GPIO_ACTIVE_LOW" and screen.focused is flags
        flags.value += " | GPIO_PULL_UP"
        # A second entry, then remove it again.
        await pilot.press("ctrl+n")
        await pilot.pause()
        assert len(editor.rows) == 2 and editor.rows[1].ref == "&gpio0"
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert len(editor.rows) == 1
        # Switch to text and back keeps the value.
        await pilot.press("ctrl+t")
        await pilot.pause()
        field = screen.query_one("#edit-value", Input)
        assert field.value == "<&gpio0 3 (GPIO_ACTIVE_LOW | GPIO_PULL_UP)>"
        await pilot.press("ctrl+t")
        await pilot.pause()
        editor = screen.query_one("#edit-value", SpecifierEditor)
        await pilot.press("ctrl+s")
        await wait_for(lambda: "gpios = <&gpio0 3 (GPIO_ACTIVE_LOW | GPIO_PULL_UP)>;"
                       in overlay.read_text())


async def test_chosen_and_label_choices(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ws = find_workspace(workspace)
    app = LazyDtsApp(AppConfig(cwd=workspace / "app", workspace=ws,
                               choice=TargetChoice(board="native_sim/native",
                                                   app=str(workspace / "app")),
                               work_dir=tmp_path / "work"))
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: "/chosen" in app.node_index)
        await wait_for(lambda: app.chosen_names)
        app.tree.move_cursor(app.node_index["/chosen"])
        props = app.query_one("#props")
        await wait_for(lambda: props.option_count and app.current_path == "/chosen")
        ids = [props.get_option_at_index(i).id for i in range(props.option_count)]
        assert "zephyr,console" in ids and ids[-1] == "new:"
        props.focus()
        props.highlighted = ids.index("new:")
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        # The name offers chosen entries from Zephyr's code.
        await pilot.press(*"shell-u")
        await pilot.pause()
        assert screen.completer.display
        await pilot.press("down", "enter")
        name = screen.query_one("#edit-name", Input)
        assert name.value == "zephyr,shell-uart"
        # The value offers node labels.
        await pilot.press("enter")
        value = screen.query_one("#edit-value", Input)
        assert screen.focused is value
        await pilot.press(*"uart")
        await pilot.pause()
        assert screen.completer.target is value and screen.completer.display
        await pilot.press("down", "enter")
        assert value.value.startswith("&uart")
        chosen = value.value
        await pilot.press("ctrl+s")
        await wait_for(lambda: f"zephyr,shell-uart = {chosen};" in overlay.read_text())
