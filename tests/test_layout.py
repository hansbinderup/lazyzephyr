"""Resizing, moving and resetting panels; the layout is remembered per tab."""

import json

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import OptionList, Static

from lazyzephyr.view import COMMON_CSS, View, layout_file


class DemoView(View):
    SPLITS = ("main", "right")
    DEFAULT_CSS = """
    DemoView #main { layout: horizontal; height: 1fr; }
    DemoView #left { width: 40%; }
    DemoView #right { width: 1fr; }
    DemoView #one, DemoView #two, DemoView #three { height: 1fr; }
    DemoView.-portrait #main { layout: vertical; }
    DemoView.-portrait #left, DemoView.-portrait #right { width: 100%; }
    DemoView.-portrait #left { height: 40%; }
    """

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="left", classes="panel"):
                yield OptionList("a", "b", id="list")
            with Vertical(id="right"):
                for name in ("one", "two", "three"):
                    with Vertical(id=name, classes="panel"):
                        yield OptionList(name, id=f"{name}-list")
        yield Static("status")


class DemoApp(App):
    CSS = COMMON_CSS

    def compose(self) -> ComposeResult:
        yield DemoView(id="view")


def ids(view, container):
    return [w.id for w in view.query_one(f"#{container}").children]


async def test_drag_border_resizes_and_is_remembered():
    app = DemoApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        view = app.query_one(DemoView)
        left = view.query_one("#left")
        assert not view.has_class("-portrait")
        before = left.outer_size.width
        border_x = left.region.right - 1
        await pilot.mouse_down(offset=(border_x, 10))
        await pilot.hover(offset=(border_x + 20, 10))
        await pilot.mouse_up(offset=(border_x + 20, 10))
        await pilot.pause()
        assert left.outer_size.width == before + 20
        saved = json.loads(layout_file().read_text())["DemoView"]["sizes"]
        assert any(k.startswith("main:horizontal:") for k in saved)
        # Dragging inside a panel (not on a border) does nothing.
        await pilot.mouse_down(offset=(border_x + 5, 10))
        await pilot.hover(offset=(border_x + 30, 10))
        await pilot.mouse_up(offset=(border_x + 30, 10))
        await pilot.pause()
        assert left.outer_size.width == before + 20
    # A new start gets the same size back.
    app = DemoApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app.query_one("#left").outer_size.width == before + 20


async def test_keys_grow_move_reset_and_layout_mode():
    app = DemoApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        view = app.query_one(DemoView)
        one, two = view.query_one("#one"), view.query_one("#two")
        view.query_one("#one-list").focus()
        start = one.outer_size.height
        await pilot.press("plus", "plus")
        await pilot.pause()
        assert one.outer_size.height == start + 4
        assert two.outer_size.height < start
        # The focused panel moves on; the order is remembered.
        await pilot.press("greater_than_sign")
        await pilot.pause()
        assert ids(view, "right") == ["two", "one", "three"]
        assert one.outer_size.height == start + 4   # its size goes with it
        # Moving the left column swaps the columns.
        view.query_one("#list").focus()
        await pilot.press("greater_than_sign")
        await pilot.pause()
        assert ids(view, "main") == ["right", "left"]
        # v: auto -> landscape -> portrait (stacked).
        await pilot.press("v", "v")
        await pilot.pause()
        assert view.has_class("-portrait") and view.layout_mode == "portrait"
    app = DemoApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        view = app.query_one(DemoView)
        assert ids(view, "right") == ["two", "one", "three"]
        assert ids(view, "main") == ["right", "left"]
        assert view.has_class("-portrait")
        # = puts sizes and order back (the mode stays).
        await pilot.press("equals_sign")
        await pilot.pause()
        await pilot.pause()
        assert ids(view, "right") == ["one", "two", "three"]
        assert ids(view, "main") == ["left", "right"]
        heights = {view.query_one(f"#{n}").outer_size.height for n in ("one", "two", "three")}
        assert max(heights) - min(heights) <= 1


async def test_hidden_panels_keep_their_own_sizes():
    app = DemoApp()
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause()
        view = app.query_one(DemoView)
        three = view.query_one("#three")
        three.display = False
        view.apply_panel_layout()
        await pilot.pause()
        view.query_one("#one-list").focus()
        await pilot.press("minus", "minus", "minus")
        await pilot.pause()
        # Showing the third panel again doesn't squash it with the two-panel sizes.
        three.display = True
        view.apply_panel_layout()
        await pilot.pause()
        assert three.outer_size.height >= 8
