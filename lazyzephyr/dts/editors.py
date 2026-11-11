"""Widgets for the property editor: a list of choices for the word being typed, and
a phandle-array editor with one row per entry (controller + named cells)."""

from __future__ import annotations

import re
from typing import Callable

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, OptionList, Select
from textual.widgets.option_list import Option

from . import macros
from .specifiers import Controller, Entry

_TOKEN = re.compile(r"&?\w*$")

Choice = tuple[str, Text]


def token_before(value: str, cursor: int) -> str:
    """The word being typed: `(GPIO_ACTIVE_LOW | GPIO_PU` -> `GPIO_PU`."""
    return _TOKEN.search(value[:cursor]).group(0)


class CompleteInput(Input):
    """An Input whose current word can be completed from a Completer.

    kind: "macro" (dt-bindings macros), "label" (node labels) or "name" (a property
    name for /chosen).
    context: words that say which macros fit (see macros.rank).
    whole: complete the whole value, not just the word being typed.
    """

    def __init__(self, *args, kind: str = "macro", context: set[str] | None = None,
                 whole: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        self.context = context or set()
        self.whole = whole

    def token(self) -> str:
        if self.whole:
            return self.value[:self.cursor_position]
        return token_before(self.value, self.cursor_position)


class Completer(OptionList):
    """Choices for the word typed in the attached CompleteInput.

    `down` in the input moves here, `enter` puts the choice in place of the word,
    `esc` (or `up` at the top) goes back to the input.
    """

    BINDINGS = [Binding("escape", "back", "Back", show=False)]
    LIMIT = 50

    def __init__(self, provider: Callable[[str, CompleteInput], list[Choice]], **kwargs):
        super().__init__(**kwargs)
        self.provider = provider
        self.target: CompleteInput | None = None
        self._inserts: list[str] = []
        self.display = False

    def attach(self, target: CompleteInput | None) -> None:
        self.target = target
        self.update_choices()

    def update_choices(self) -> None:
        t = self.target
        items = self.provider(t.token(), t)[:self.LIMIT] if t is not None else []
        self.clear_options()
        self._inserts = [i for i, _ in items]
        self.add_options([Option(d) for _, d in items])
        self.display = bool(items)

    def action_back(self) -> None:
        if self.target is not None:
            self.target.focus()

    def action_cursor_up(self) -> None:
        if self.highlighted in (None, 0):
            self.action_back()
        else:
            super().action_cursor_up()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.insert(event.option_index)

    def insert(self, index: int) -> None:
        t = self.target
        if t is None or not 0 <= index < len(self._inserts):
            return
        before, after = t.value[:t.cursor_position], t.value[t.cursor_position:]
        new = before[:len(before) - len(t.token())] + self._inserts[index]
        t.value = new + after
        t.cursor_position = len(new)
        t.focus()


def macro_choice(m: macros.Macro) -> Choice:
    t = Text()
    t.append(m.name + (m.args or ""), style="bold")
    t.append(f"  {m.value[:40]}", style="bright_black")
    t.append(f"  {m.header.parent.name}/{m.header.name}", style="bright_black italic")
    return m.insert, t


class SpecRow(Horizontal):
    """One phandle-array entry: the controller and an input per cell."""

    def __init__(self, ctrls: list[Controller], ref: str | None, cells: list[str],
                 context: set[str]):
        super().__init__(classes="spec-row")
        self.ctrls = ctrls
        self.by_ref = {c.ref: c for c in ctrls}
        self.ref = ref if ref in self.by_ref else None
        self._initial = cells
        self.context = context

    def compose(self) -> ComposeResult:
        yield Select([(c.title, c.ref) for c in self.ctrls],
                     value=self.ref if self.ref else Select.BLANK,
                     prompt="controller", classes="spec-ctrl")
        yield from self._cell_inputs(self._initial)

    def _cell_inputs(self, vals: list[str]) -> list[CompleteInput]:
        ctrl = self.by_ref.get(self.ref)
        if ctrl is None:
            return []
        out = []
        for i, name in enumerate(ctrl.cells):
            hint = f"{name} (0-{ctrl.limit - 1})" if name == "pin" and ctrl.limit else name
            out.append(CompleteInput(value=vals[i] if i < len(vals) else "", placeholder=hint,
                                     kind="macro", classes="spec-cell",
                                     context=self.context | macros.context_words(name,
                                                                                 ctrl.compat)))
        return out

    @on(Select.Changed, ".spec-ctrl")
    async def controller_changed(self, event: Select.Changed) -> None:
        event.stop()
        new = event.value if isinstance(event.value, str) else None
        if new == self.ref:
            return
        vals = [i.value for i in self.query(".spec-cell").results(Input)]
        self.ref = new
        await self.query(".spec-cell").remove()
        await self.mount_all(self._cell_inputs(vals))

    def entry(self) -> Entry:
        return self.ref or "", [i.value for i in self.query(".spec-cell").results(Input)]


class SpecifierEditor(Vertical):
    """A phandle-array as rows of controller + cells."""

    def __init__(self, ctrls: list[Controller], entries: list[Entry], context: set[str],
                 **kwargs):
        super().__init__(**kwargs)
        self.ctrls, self._initial, self.context = ctrls, entries, context

    def compose(self) -> ComposeResult:
        for ref, cells in self._initial or [(None, [])]:
            yield SpecRow(self.ctrls, ref, cells, self.context)

    @property
    def rows(self) -> list[SpecRow]:
        return list(self.query(SpecRow))

    def entries(self) -> list[Entry]:
        return [r.entry() for r in self.rows]

    async def add_row(self) -> None:
        rows = self.rows
        ref = rows[-1].ref if rows else None
        row = SpecRow(self.ctrls, ref, [], self.context)
        await self.mount(row)
        cells = row.query(".spec-cell")
        (cells.first() if cells else row.query_one(Select)).focus()

    async def remove_row(self, row: SpecRow) -> bool:
        rows = self.rows
        if row not in rows or len(rows) == 1:
            return False
        i = rows.index(row)
        await row.remove()
        rest = self.rows
        rest[min(i, len(rest) - 1)].query_one(Select).focus()
        return True
