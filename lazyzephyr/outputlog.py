"""The Output panels' log: a RichLog whose text can be selected and copied.

RichLog draws its lines itself, so Textual's mouse selection can't reach them
on its own. This adds what it needs: the offsets that map screen cells back to
the text, the selected text, and the selection's highlight. ctrl+c copies the
selection, or the whole output when nothing is selected.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from rich.segment import Segment
from rich.style import Style
from textual.binding import Binding, BindingType
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import RichLog


class OutputLog(RichLog):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("ctrl+c", "copy_output", "Copy")]

    def __init__(self, *, full_text: Callable[[], str] | None = None, **kwargs) -> None:
        """`full_text` gives what ctrl+c copies with nothing selected (default: the shown lines).

        It lets a panel copy more than it shows, such as a job's lines that were
        never written to it.
        """
        kwargs.setdefault("markup", False)
        kwargs.setdefault("highlight", False)
        kwargs.setdefault("wrap", False)
        super().__init__(**kwargs)
        self.full_text = full_text

    def shown_text(self) -> str:
        return "\n".join(line.text.rstrip() for line in self.lines)

    def action_copy_output(self) -> None:
        if self.text_selection is not None:
            text = self.screen.get_selected_text()
            self.screen.clear_selection()
            if text:
                self.app.copy_to_clipboard(text)
                self.notify("Copied the selection", timeout=2)
            return
        text = self.full_text() if self.full_text is not None else self.shown_text()
        if not text.strip():
            self.notify("no output to copy", timeout=2)
            return
        self.app.copy_to_clipboard(text)
        lines = text.count("\n") + 1
        self.notify(f"Copied the output ({lines} line{'s' if lines != 1 else ''})", timeout=2)

    # -- mouse selection ------------------------------------------------------

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        return selection.extract(self.shown_text()), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self._line_cache.clear()
        self.refresh()

    def _render_line(self, y: int, scroll_x: int, width: int) -> Strip:
        selection = self.text_selection
        span = selection.get_span(y) if selection and y < len(self.lines) else None
        if span is None:
            line = super()._render_line(y, scroll_x, width)
        else:
            line = self.lines[y]
            start, end = span
            end = line.cell_length if end == -1 else end
            # Only its background: its colour resolves to the same as the
            # background here, which would hide the text.
            style = Style(
                bgcolor=self.screen.get_component_rich_style("screen--selection").bgcolor,
                reverse=False,
            )
            selected = [
                Segment(text, (seg_style + style) if seg_style else style, control)
                for text, seg_style, control in line.crop(start, end)
            ]
            line = Strip.join([line.crop(0, start), Strip(selected), line.crop(end)])
            line = line.crop_extend(scroll_x, scroll_x + width, self.rich_style)
        # Offsets let the mouse select: they map screen cells back to the text.
        return line.apply_offsets(scroll_x, y)
