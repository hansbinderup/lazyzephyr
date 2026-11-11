"""Drive the Kconfig tab headlessly against the fake build."""

import asyncio

from lazyzephyr.dts.app import ConfirmScreen
from lazyzephyr.dts.editors import PickScreen
from lazyzephyr.dts.workspace import find_workspace
from lazyzephyr.kconfig.app import AppConfig, KProblemsScreen, LazyKconfigApp


async def wait_for(predicate, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


def make_app(workspace, kbuild, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return LazyKconfigApp(AppConfig(cwd=workspace / "app", workspace=find_workspace(workspace),
                                    build_dir=kbuild))


async def test_search_and_turn_on_with_dependencies(workspace, kbuild, tmp_path, monkeypatch):
    app = make_app(workspace, kbuild, tmp_path, monkeypatch)
    prj = kbuild.parent / "prj.conf"
    view = app.view
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: view.session is not None and view.items)
        status = str(app.query_one("#kstatus").render())
        assert "1 problem(s)" in status
        # Search, then space on the result: it needs I2C, so it asks.
        await pilot.press("slash", *"i2c sh")
        await pilot.pause()
        assert view.results[0].name == "I2C_SHELL"
        await pilot.press("enter")  # into the results
        await pilot.press("space")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await wait_for(lambda: "CONFIG_I2C=y" in prj.read_text())
        assert view.session.symbol("I2C_SHELL").str_value == "y"
        await wait_for(lambda: "not built yet" in str(app.query_one("#kstatus").render()))
        # The related list goes to a dependency; backspace comes back.
        details = str(app.query_one("#kdetails").render())
        assert "CONFIG_I2C_SHELL" in details and "Set in your" in details
        related = app.query_one("#krelated")
        related.focus()
        related.highlighted = view.related_rows.index("I2C")
        await pilot.press("enter")
        await wait_for(lambda: getattr(view.current, "name", None) == "I2C")
        await pilot.press("backspace")
        await wait_for(lambda: getattr(view.current, "name", None) == "I2C_SHELL")
        # u undoes.
        await pilot.press("u")
        await wait_for(lambda: prj.read_text() == "CONFIG_I2C_SHELL=y\n")


async def test_tree_values_and_problems(workspace, kbuild, tmp_path, monkeypatch):
    app = make_app(workspace, kbuild, tmp_path, monkeypatch)
    prj = kbuild.parent / "prj.conf"
    view = app.view
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: view.session is not None and view.items)
        sym = view.session.symbol("NAME")
        assert view.reveal_symbol(sym)
        await wait_for(lambda: getattr(view.current, "name", None) == "NAME")
        # A string: type the new value.
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, PickScreen)
        await pilot.press(*"sensor-board", "enter")
        await wait_for(lambda: 'CONFIG_NAME="sensor-board"' in prj.read_text())
        # d removes it again: the board's value applies.
        await pilot.press("d")
        await wait_for(lambda: "CONFIG_NAME" not in prj.read_text())
        assert view.session.symbol("NAME").str_value == "board"
        # A choice: pick from its options.
        choice = view.session.symbol("LOG_MODE_DEFERRED").choice
        view.reveal_node(choice.nodes[0])
        await wait_for(lambda: view.current is choice)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press(*"immed", "enter")
        await wait_for(lambda: "CONFIG_LOG_MODE_IMMEDIATE=y" in prj.read_text())
        # Hidden options (deps not met) only show with A.
        assert not view.reveal_symbol(view.session.symbol("SPEED"))
        await pilot.press("A")
        await pilot.pause()
        assert view.reveal_symbol(view.session.symbol("SPEED"))
        # p lists the line that has no effect; enter goes to the option.
        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, KProblemsScreen)
        await pilot.press("enter")
        await wait_for(lambda: getattr(view.current, "name", None) == "I2C_SHELL")
