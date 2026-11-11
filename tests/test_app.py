"""Drive the TUI headlessly against a throwaway workspace."""

import asyncio

from lazydts.app import AppConfig, LazyDtsApp
from lazydts.state import TargetChoice
from lazydts.workspace import find_workspace


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
