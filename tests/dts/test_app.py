"""Drive the TUI headlessly against a throwaway workspace."""

import asyncio

from textual.widgets import Input

from lazyzephyr.dts.app import AppConfig, LazyDtsApp, prop_id
from lazyzephyr.dts.editors import PickScreen, SpecifierEditor
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


def make_app(workspace, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ws = find_workspace(workspace)
    return LazyDtsApp(AppConfig(cwd=workspace / "app", workspace=ws,
                                choice=TargetChoice(board="native_sim/native",
                                                    app=str(workspace / "app")),
                                work_dir=tmp_path / "work"))


def setting_ids(app):
    props = app.query_one("#props")
    return [props.get_option_at_index(i).id for i in range(props.option_count)]


async def open_settings(app, pilot, path):
    await wait_for(lambda: path in app.node_index)
    app.reveal(path)
    await wait_for(lambda: app.current_path == path)
    app.tree.focus()
    await pilot.press("enter")  # enter on a tree node goes to its settings
    await wait_for(lambda: app.focused is app.query_one("#props"))


def pick_row(app, row_id):
    props = app.query_one("#props")
    props.highlighted = setting_ids(app).index(row_id)


async def test_chosen_and_label_choices(workspace, tmp_path, monkeypatch):
    app = make_app(workspace, tmp_path, monkeypatch)
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: app.chosen_names)
        await open_settings(app, pilot, "/chosen")
        ids = setting_ids(app)
        assert "zephyr,console" in ids and ids[-1] == "new:"
        pick_row(app, "new:")
        await pilot.press("enter")
        await pilot.pause()
        # First the name: Zephyr's chosen names are listed, the match is picked.
        assert isinstance(app.screen, PickScreen)
        await pilot.press(*"shell-u")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        # Then the node it points at, from the labels.
        screen = app.screen
        assert isinstance(screen, PickScreen) and screen.title_text == "zephyr,shell-uart"
        await pilot.press(*"uart")
        await pilot.pause()
        await pilot.press("enter")
        await wait_for(lambda: "zephyr,shell-uart = &uart0;" in overlay.read_text())


async def test_settings_list(workspace, tmp_path, monkeypatch):
    app = make_app(workspace, tmp_path, monkeypatch)
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: any(p.startswith("/i2c@") for p in app.node_index))
        i2c = next(p for p in app.node_index if p.startswith("/i2c@"))
        await open_settings(app, pilot, i2c)
        ids = setting_ids(app)
        # Enabled first, the controller's own settings shown, generic ones under more.
        assert ids[0] == "status" and "clock-frequency" in ids
        assert "interrupts" not in ids and "more:" in ids and "add:" in ids
        # space turns it off and on.
        pick_row(app, "status")
        await pilot.press("space")
        await wait_for(lambda: 'status = "disabled";' in overlay.read_text())
        await pilot.press("space")
        await wait_for(lambda: 'status = "okay";' in overlay.read_text())
        # Clock frequency offers the I2C speeds, by name and in Hz.
        pick_row(app, "clock-frequency")
        await pilot.press("enter")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, PickScreen)
        lst = screen.query_one("#pick-list")
        first = str(lst.get_option_at_index(0).prompt)
        assert first.startswith("I2C_BITRATE_STANDARD") and "100 kHz" in first
        await pilot.press("down", "enter")
        await wait_for(lambda: "clock-frequency = <I2C_BITRATE_FAST>;" in overlay.read_text())
        await wait_for(lambda: "400 kHz" in str(
            app.query_one("#props").get_option_at_index(
                setting_ids(app).index("clock-frequency")).prompt))
        # Typing a number works too.
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*"250000", "enter")
        await wait_for(lambda: "clock-frequency = <250000>;" in overlay.read_text())
        # d removes it, without asking (u undoes).
        await pilot.press("d")
        await wait_for(lambda: "clock-frequency" not in overlay.read_text())
        await pilot.press("u")
        await wait_for(lambda: "clock-frequency = <250000>;" in overlay.read_text())
        # "more settings" shows the rest; esc goes back to the tree.
        pick_row(app, "more:")
        await pilot.press("enter")
        await wait_for(lambda: "interrupts" in setting_ids(app))
        await pilot.press("escape")
        assert app.focused is app.tree


async def test_enum_and_children(workspace, tmp_path, monkeypatch):
    app = make_app(workspace, tmp_path, monkeypatch)
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    overlay.write_text(overlay.read_text() + """
&i2c0 {
	widget: widget@10 {
		compatible = "vnd,widget";
		reg = <0x10>;
		mode = "fast";
	};
};
""")
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: any(p.endswith("/widget@10") for p in app.node_index))
        i2c = next(p for p in app.node_index if p.startswith("/i2c@") and "/" not in p[1:])
        await open_settings(app, pilot, i2c)
        # The bus lists its devices; enter goes to one.
        pick_row(app, f"node:{i2c}/widget@10")
        await pilot.press("enter")
        await wait_for(lambda: app.current_path == f"{i2c}/widget@10")
        await wait_for(lambda: "mode" in setting_ids(app))
        # An enum is a list to pick from.
        pick_row(app, "mode")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PickScreen)
        await pilot.press(*"slo", "enter")
        await wait_for(lambda: 'mode = "slow";' in overlay.read_text())


async def test_add_node_with_odd_property_names(workspace, tmp_path, monkeypatch):
    app = make_app(workspace, tmp_path, monkeypatch)
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: app.bindings_catalog)
        i2c = next(p for p in app.node_index if p.startswith("/i2c@"))
        app.tree.move_cursor(app.node_index[i2c])
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        await pilot.press(*"vnd,expander")
        await pilot.pause()
        await pilot.press("enter")  # the form used to crash on '#gpio-cells'
        await pilot.pause()
        await pilot.press(*"0x20")
        form = app.screen
        form.query_one(f"#{prop_id('#gpio-cells')}").value = "2"
        form.query_one(f"#{prop_id('vnd,mode')}").value = "a"
        await pilot.press("ctrl+s")
        await wait_for(lambda: "expander@20 {" in overlay.read_text())
        text = overlay.read_text()
        assert "#gpio-cells = <2>;" in text and 'vnd,mode = "a";' in text
