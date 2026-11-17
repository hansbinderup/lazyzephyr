"""The Output panels' log: mouse selection and ctrl+c copy."""

from rich.text import Text
from textual.app import App

from lazyzephyr.outputlog import OutputLog


class LogApp(App):
    def __init__(self, **kwargs):
        super().__init__()
        self.log_kwargs = kwargs
        self.copied: list[str] = []

    def compose(self):
        yield OutputLog(id="out", **self.log_kwargs)

    def copy_to_clipboard(self, text: str) -> None:
        self.copied.append(text)


async def test_ctrl_c_copies_the_selection_or_everything():
    app = LogApp()
    async with app.run_test(size=(40, 6)) as pilot:
        log = app.query_one(OutputLog)
        log.write(Text("-- west build: building", style="cyan"))
        log.write("[1/2] Compiling main.c")
        log.write("[2/2] Linking zephyr.elf")
        log.focus()
        await pilot.pause()

        await pilot.press("ctrl+c")  # nothing selected: all of it
        assert app.copied == [
            "-- west build: building\n[1/2] Compiling main.c\n[2/2] Linking zephyr.elf"
        ]

        await pilot.mouse_down(log, offset=(3, 0))
        await pilot.hover(log, offset=(4, 1))
        await pilot.mouse_up(log, offset=(4, 1))
        await pilot.pause()
        assert app.screen.get_selected_text() == "west build: building\n[1/2]"
        # The selection keeps the text readable: its own colour on the selection's background.
        selection_bg = app.screen.get_component_rich_style("screen--selection").bgcolor
        for y in (0, 1):
            cell = next(s.style for s in log.render_line(y) if s.style.bgcolor == selection_bg)
            assert cell.color is not None and cell.color != cell.bgcolor

        await pilot.press("ctrl+c")
        assert app.copied[-1] == "west build: building\n[1/2]"
        assert log.text_selection is None


async def test_full_text_copies_more_than_is_shown():
    app = LogApp(full_text=lambda: "line 1\nline 2\nline 3")
    async with app.run_test(size=(40, 6)) as pilot:
        log = app.query_one(OutputLog)
        log.write("line 3")
        log.focus()
        await pilot.press("ctrl+c")
        assert app.copied == ["line 1\nline 2\nline 3"]


async def test_nothing_to_copy():
    app = LogApp()
    async with app.run_test(size=(40, 6)) as pilot:
        app.query_one(OutputLog).focus()
        await pilot.press("ctrl+c")
        assert app.copied == []
