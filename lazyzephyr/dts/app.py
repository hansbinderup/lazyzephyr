"""The devicetree tab: browse and edit a target's devicetree."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, OptionList, Select, Static, Tree
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from . import catalog, checks, macros, settings, specifiers, values
from .catalog import BindingInfo
from .editors import (CompleteInput, Completer, PickScreen, SpecifierEditor, SpecRow,
                      macro_choice)
from .model import import_edtlib
from .session import REF_NODES, EditResult, Session
from .state import TargetChoice, save_choice
from ..view import COMMON_CSS, ForwardActions, View
from .workspace import (Board, DtInputs, Workspace, find_board, find_boards, find_build_dirs,
                        inputs_for_board, inputs_from_build)


@dataclass
class AppConfig:
    cwd: Path
    workspace: Workspace
    choice: TargetChoice | None = None
    work_dir: Path | None = None
    # Open the target picker on start.
    pick_target: bool = False
    extra: dict = field(default_factory=dict)


def resolve_choice(ws: Workspace, choice: TargetChoice) -> DtInputs:
    """Raises LookupError."""
    if choice.build_dir:
        return inputs_from_build(Path(choice.build_dir))
    board = find_board(ws, choice.board or "")
    return inputs_for_board(ws, board, Path(choice.app) if choice.app else None,
                            list(choice.shields))


def raw_value(prop) -> str:
    """A dtlib property's value as DTS text, e.g. `<0x1>`."""
    text = str(prop).strip().rstrip(";")
    _, eq, value = text.partition("=")
    return " ".join(value.split()) if eq else "true"


def rel(path: Path | str, base: Path) -> str:
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------


class NodeTree(ForwardActions, Tree[str]):
    BINDINGS = [
        Binding("enter", "focus_settings", "Settings"),
        Binding("a", "add_node", "Add node"),
        Binding("s", "toggle_status", "okay/disabled"),
        Binding("d", "delete_node", "Delete"),
        Binding("o", "open_editor", "Open"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("l,right", "expand", show=False),
        Binding("h,left", "collapse", show=False),
    ]

    def action_expand(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.allow_expand and not node.is_expanded:
            node.expand()
        elif node.children:
            self.move_cursor(node.children[0])

    def action_collapse(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.is_expanded:
            node.collapse()
        elif node.parent is not None and node.parent is not self.root:
            self.move_cursor(node.parent)


class PropList(ForwardActions, OptionList):
    """A node's settings (see settings.rows)."""

    BINDINGS = [
        Binding("enter,e", "edit_prop", "Change"),
        Binding("space", "toggle_prop", "On/off"),
        Binding("d", "delete_prop", "Remove"),
        Binding("a", "add_node", "Add node"),
        Binding("o", "open_editor", "Open file"),
        Binding("escape,h,left", "focus_tree", "Back to tree"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]


class Panel(Vertical):
    ALLOW_MAXIMIZE = True


class ScrollPanel(VerticalScroll):
    ALLOW_MAXIMIZE = True


class FilterInput(ForwardActions, Input):
    BINDINGS = [Binding("escape", "clear_filter", "Clear filter")]


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------


def prop_id(name: str) -> str:
    """A widget id for property `name` (ids can't hold `#` or `,`, as in
    `#gpio-cells` or `vnd,mode`)."""
    return "prop-" + re.sub(r"[^A-Za-z0-9_-]", "_", name)


def file_options(files: list[Path], base: Path, user: list[Path]) -> list[tuple[str, Path]]:
    return [(rel(f, base) + ("  (app)" if f in user else ""), f) for f in files]


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y,enter", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, title: str, message: str):
        super().__init__()
        self.title_text, self.message = title, message

    def compose(self) -> ComposeResult:
        with Vertical(id="dts-confirm-dialog", classes="panel dialog") as dialog:
            dialog.border_title = self.title_text
            dialog.border_subtitle = "y: yes · n: no"
            yield Static(self.message)

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "dismiss_help", "Close")]

    TEXT = """\
[b]Tree[/b]
  j/k, arrows        move
  l/h, space         unfold / fold
  enter              go to the node's settings
  /                  search by name, label or compatible
  a                  add a node under the selected node
  s                  turn the node on / off
  d                  delete the node
  o                  open its file in $EDITOR

[b]Settings[/b]
  enter              change the setting: pick from the list or type a value
  space              switch on/off settings (and Enabled)
  d                  remove the value (the board's value applies again)
  a                  add a node here
  esc                back to the tree
  The settings that are set come first; "more settings" shows the rest.

[b]Picking a value[/b]
  type               search the list, or type your own value
  up/down, enter     pick
  esc                cancel

[b]Anywhere[/b]
  p                  problems: pins used twice, two devices at one address,
                     overlapping partitions, enabled nodes needing disabled ones
  w                  pick the file edits go to
  t                  pick another target
  u                  undo the last edit
  r                  reload
  z                  maximize the focused panel
  q                  quit

Changes go straight into your overlay (or the file that already sets the value),
then the devicetree is checked. If it no longer parses, the change is put back
and you see why. u undoes the last change.
"""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="dts-help-dialog", classes="panel dialog") as dialog:
            dialog.border_title = "Devicetree help"
            yield Static(self.TEXT)

    def action_dismiss_help(self) -> None:
        self.dismiss(None)


class ProblemsScreen(ModalScreen[checks.Problem | None]):
    """Everything that parses but looks wrong; enter goes to the node."""

    BINDINGS = [Binding("escape,q,p", "cancel", "Close")]

    def __init__(self, problems: list[checks.Problem]):
        super().__init__()
        self.problems = problems

    def compose(self) -> ComposeResult:
        with Vertical(id="problems-dialog", classes="panel dialog") as dialog:
            dialog.border_title = f"Problems ({len(self.problems)})"
            dialog.border_subtitle = "enter: go to the node · esc: close"
            if not self.problems:
                yield Static("Nothing found: no pin is used twice, no two devices share an "
                             "address, partitions don't overlap and no enabled node needs a "
                             "disabled one.", classes="hint")
                return
            yield OptionList(*[Option(problem_text(p), id=str(i))
                               for i, p in enumerate(self.problems)], id="problems-list")

    @on(OptionList.OptionSelected, "#problems-list")
    def picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.problems[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


def problem_text(p: checks.Problem) -> Text:
    t = Text()
    if p.severity == "error":
        t.append("✖ ", style="red")
    else:
        t.append("⚠ ", style="yellow")
    t.append(p.message)
    return t


class FileScreen(ModalScreen[Path | None]):
    """Pick the file edits go to."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, files: list[Path], current: Path | None, base: Path, user: list[Path]):
        super().__init__()
        self.files, self.current, self.base, self.user = files, current, base, user

    def compose(self) -> ComposeResult:
        with Vertical(id="file-dialog", classes="panel dialog") as dialog:
            dialog.border_title = "Write edits to"
            dialog.border_subtitle = "enter: pick · esc: cancel"
            yield Static("Files that come later override earlier ones. Zephyr's own and "
                         "third-party module files are not offered.", classes="hint")
            opts = [Option(("● " if f == self.current else "  ") + label, id=str(f))
                    for label, f in file_options(self.files, self.base, self.user)]
            yield OptionList(*opts, id="file-list")

    def on_mount(self) -> None:
        lst = self.query_one(OptionList)
        if self.current in self.files:
            lst.highlighted = self.files.index(self.current)

    @on(OptionList.OptionSelected)
    def picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(Path(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class TargetScreen(ModalScreen[TargetChoice | None]):
    """Pick an existing build directory or a board."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, ws: Workspace, cwd: Path, current: TargetChoice | None):
        super().__init__()
        self.ws, self.cwd, self.current = ws, cwd, current
        self.builds: list[Path] = []
        self.boards: list[Board] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="target-dialog", classes="panel dialog") as dialog:
            dialog.border_title = "Target"
            dialog.border_subtitle = "type to filter · enter: pick · esc: cancel"
            yield Static("A build directory is exact (uses its build_info.yml). A board "
                         "uses the app and shields below.", classes="hint")
            yield Input(placeholder="filter boards and builds", id="target-filter")
            yield OptionList(id="target-list")
            with Horizontal(id="target-extra"):
                app = self.current.app if self.current and self.current.app else (
                    str(self.cwd) if (self.cwd / "CMakeLists.txt").is_file() else "")
                yield Input(value=app, placeholder="app directory (optional)",
                            id="target-app")
                yield Input(value=", ".join(self.current.shields if self.current else []),
                            placeholder="shields, comma separated", id="target-shields")

    def on_mount(self) -> None:
        self.query_one("#target-list", OptionList).loading = True
        self.load()

    @work(thread=True)
    def load(self) -> None:
        builds = find_build_dirs(self.ws, self.cwd)
        boards = find_boards(self.ws)
        self.app.call_from_thread(self._loaded, builds, boards)

    def _loaded(self, builds: list[Path], boards: list[Board]) -> None:
        self.builds, self.boards = builds, boards
        lst = self.query_one("#target-list", OptionList)
        lst.loading = False
        self.refill()
        self.query_one("#target-filter", Input).focus()

    @on(Input.Changed, "#target-filter")
    def refill(self, _=None) -> None:
        words = self.query_one("#target-filter", Input).value.lower().split()
        lst = self.query_one("#target-list", OptionList)
        lst.clear_options()
        opts = []
        for b in self.builds:
            text = rel(b, self.ws.topdir)
            if all(w in text.lower() or w in "build" for w in words):
                label = Text.assemble(("build  ", "magenta"), text)
                opts.append(Option(label, id=f"build:{b}"))
        for board in self.boards:
            hay = f"{board.target} {board.vendor or ''} {board.full_name or ''}".lower()
            if all(w in hay for w in words):
                label = Text.assemble(("board  ", "cyan"), board.target,
                                      (f"  {board.full_name or ''}", "bright_black"))
                opts.append(Option(label, id=f"board:{board.target}"))
            if len(opts) > 400:
                break
        lst.add_options(opts)
        if opts:
            lst.highlighted = 0

    @on(Input.Submitted, "#target-filter")
    def submit_filter(self) -> None:
        lst = self.query_one("#target-list", OptionList)
        if lst.highlighted is not None:
            self._pick(lst.get_option_at_index(lst.highlighted).id)

    def key_down(self) -> None:
        if self.focused is self.query_one("#target-filter"):
            self.query_one("#target-list").focus()

    @on(OptionList.OptionSelected, "#target-list")
    def picked(self, event: OptionList.OptionSelected) -> None:
        self._pick(event.option.id)

    def _pick(self, option_id: str) -> None:
        kind, _, value = option_id.partition(":")
        if kind == "build":
            self.dismiss(TargetChoice(build_dir=value))
            return
        app = self.query_one("#target-app", Input).value.strip()
        shields = [s.strip() for s in self.query_one("#target-shields", Input).value.split(",")
                   if s.strip()]
        if app:
            app = str((self.cwd / app).resolve())
        self.dismiss(TargetChoice(board=value, app=app or None, shields=shields))

    def action_cancel(self) -> None:
        self.dismiss(None)


class EditScreen(ModalScreen[EditResult | None]):
    """Edit a phandle-array (gpios, pwms…): one row per entry, a controller and its
    named cells. Everything else is edited with a PickScreen."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "apply", "Apply", priority=True),
        Binding("ctrl+n", "add_entry", "Add entry", priority=True),
        Binding("ctrl+r", "remove_entry", "Remove entry", priority=True),
        Binding("ctrl+t", "toggle_text", "Type as text", priority=True),
    ]

    def __init__(self, session: Session, node_path: str, name: str, file: Path):
        super().__init__()
        self.session, self.node_path, self.prop, self.file = session, node_path, name, file
        self.spec = session.spec(session.node(node_path), name)
        self.space = specifiers.space(name, self.spec)
        self.ctrls = specifiers.controllers(session.model.edt, self.space)

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="edit-dialog", classes="panel dialog") as dialog:
            dialog.border_title = settings.friendly(self.prop)
            yield Static(self._info(), classes="hint")
            current = settings.source_text(self.session, self.node_path, self.prop)
            yield self._value_widget(current, structured=True)
            yield Completer(self._choices, id="edit-complete")
            yield Static("", id="edit-error")

    def on_mount(self) -> None:
        self._update_keys()
        widget = self.query_one("#edit-value")
        cells = widget.query(Input) if isinstance(widget, SpecifierEditor) else None
        if cells:
            cells.first().focus()
        elif isinstance(widget, SpecifierEditor):
            widget.query_one(Select).focus()
        else:
            widget.focus()

    def _value_widget(self, text: str | None, structured: bool):
        if structured and self.ctrls:
            entries = specifiers.parse(text, self.ctrls)
            if entries is not None:
                return SpecifierEditor(self.ctrls, entries, self._macro_context(),
                                       id="edit-value")
        value = values.to_input(self.spec.type, text) if text is not None else ""
        return CompleteInput(value=value, placeholder="e.g. <&gpio0 5 GPIO_ACTIVE_LOW>",
                             id="edit-value", kind="macro", context=self._macro_context())

    def _macro_context(self) -> set[str]:
        node = self.session.node(self.node_path)
        return macros.context_words(self.prop, self.space, *node.compats[:1], *node.buses,
                                    *node.on_buses)

    def _update_keys(self) -> None:
        if isinstance(self.query_one("#edit-value"), SpecifierEditor):
            keys = ["enter: set", "ctrl+n: another entry", "ctrl+r: remove entry",
                    "ctrl+t: type it", "esc: cancel"]
        else:
            keys = ["enter: set", "ctrl+t: fields" if self.ctrls else "", "esc: cancel"]
        self.query_one("#edit-dialog").border_subtitle = " · ".join(k for k in keys if k)

    def _choices(self, token: str, target: CompleteInput) -> list:
        if token[:1].isdigit():
            return []
        return [macro_choice(x)
                for x in macros.rank(self.session.model.macros, token, target.context)]

    def _info(self) -> str:
        desc = " ".join((self.spec.description or "").split())
        hint = ("Pick the controller, then fill in each field. While typing a field, "
                "↓ shows the matching macros.")
        if not self.ctrls:
            hint = f"No node in this tree can be used here (none has #{self.space}-cells)."
        return escape(f"{desc}\n{hint}" if desc else hint)

    # -- events ----------------------------------------------------------

    @property
    def completer(self) -> Completer:
        return self.query_one("#edit-complete", Completer)

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.action_apply()

    @on(Input.Changed)
    def changed(self, event: Input.Changed) -> None:
        if event.input is self.completer.target:
            self.completer.update_choices()

    def on_descendant_focus(self, event) -> None:
        widget, completer = event.widget, self.completer
        if widget is not completer:
            completer.attach(widget if isinstance(widget, CompleteInput) else None)

    def on_key(self, event) -> None:
        completer = self.completer
        if event.key == "down" and isinstance(self.focused, CompleteInput) and \
                completer.display and completer.option_count:
            event.stop()
            completer.focus()
            completer.highlighted = 0
        elif event.key == "enter" and not isinstance(self.focused, (Input, OptionList)) and \
                not (isinstance(self.focused, Select) and self.focused.expanded):
            event.stop()
            self.action_apply()

    # -- actions ---------------------------------------------------------

    def _error(self, message: str) -> None:
        self.query_one("#edit-error", Static).update(f"[red]{escape(message)}[/red]")

    async def action_add_entry(self) -> None:
        widget = self.query_one("#edit-value")
        if isinstance(widget, SpecifierEditor):
            await widget.add_row()

    async def action_remove_entry(self) -> None:
        widget = self.query_one("#edit-value")
        if not isinstance(widget, SpecifierEditor):
            return
        focused = self.focused
        row = next((a for a in focused.ancestors_with_self if isinstance(a, SpecRow)),
                   None) if focused else None
        if not await widget.remove_row(row or widget.rows[-1]):
            self._error("can't remove the only entry; press d in the list to remove the "
                        "setting instead")

    async def action_toggle_text(self) -> None:
        widget = self.query_one("#edit-value")
        if not self.ctrls:
            return
        if isinstance(widget, SpecifierEditor):
            text = specifiers.compose([e for e in widget.entries() if e[0]])
            new = self._value_widget(text, structured=False)
        else:
            text = widget.value.strip()
            if text and "<" not in text:
                text = f"<{text}>"
            if specifiers.parse(text, self.ctrls) is None:
                self._error("can't split this into controllers and fields; edit it as text")
                return
            new = self._value_widget(text, structured=True)
        self._error("")
        await widget.remove()
        await self.query_one("#edit-dialog").mount(new, before=self.completer)
        self._update_keys()
        (new.query(Input).first() if isinstance(new, SpecifierEditor) and new.query(Input)
         else new.query_one(Select) if isinstance(new, SpecifierEditor) else new).focus()

    def action_apply(self) -> None:
        widget = self.query_one("#edit-value")
        if isinstance(widget, SpecifierEditor):
            entries = widget.entries()
            if err := specifiers.check(entries, self.ctrls):
                self._error(err)
                return
            text = specifiers.compose(entries)
        else:
            text = widget.value
        result = self.session.set_property(self.node_path, self.prop, text, self.file)
        if result.ok:
            self.dismiss(result)
        else:
            self._error(result.message)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AddScreen(ModalScreen[BindingInfo | None]):
    """Search every binding, pick one to add under the selected node."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+f", "toggle_fit", "Fits here only"),
        Binding("ctrl+d", "toggle_driver", "With driver only"),
    ]

    def __init__(self, bindings: list[BindingInfo], parent_path: str, parent_buses: list[str]):
        super().__init__()
        self.bindings = bindings
        self.parent_path = parent_path
        self.buses = parent_buses
        self.fit_only = True
        self.driver_only = False
        self.shown: list[BindingInfo] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="add-dialog", classes="panel dialog") as dialog:
            dialog.border_title = f"Add under {self.parent_path}"
            dialog.border_subtitle = ("enter: pick · ctrl+f: fits here only · "
                                      "ctrl+d: with driver only · esc: cancel")
            yield Static("", id="add-mode", classes="hint")
            yield Input(placeholder="search compatibles and descriptions", id="add-filter")
            with Horizontal(id="add-body"):
                yield OptionList(id="add-list")
                yield VerticalScroll(Static("", id="add-info"), id="add-info-panel")

    def on_mount(self) -> None:
        self.refill()
        self.query_one("#add-filter", Input).focus()

    def _fits(self, b: BindingInfo) -> bool:
        if self.buses:
            return b.on_bus in self.buses
        return b.on_bus is None

    @on(Input.Changed, "#add-filter")
    def refill(self, _=None) -> None:
        words = self.query_one("#add-filter", Input).value.lower().split()
        mode = []
        if self.fit_only:
            mode.append(f"only bindings for a {'/'.join(self.buses)} bus" if self.buses
                        else "only bindings that are not on a bus")
        if self.driver_only:
            mode.append("only with a driver")
        self.query_one("#add-mode", Static).update(
            ("Showing " + ", ".join(mode) if mode else "Showing all bindings")
            + f"  ·  ✓ = a driver uses it")
        shown = []
        for b in self.bindings:
            if self.fit_only and not self._fits(b):
                continue
            if self.driver_only and not b.has_driver:
                continue
            hay = f"{b.compatible} {b.summary}".lower()
            if all(w in hay for w in words):
                shown.append(b)
        # Exact/prefix matches on the compatible first.
        if words:
            shown.sort(key=lambda b: (not b.compatible.lower().startswith(words[0])
                                      and words[0] not in b.compatible.split(",")[-1],
                                      b.compatible))
        self.shown = shown[:500]
        lst = self.query_one("#add-list", OptionList)
        lst.clear_options()
        lst.add_options([self._option(i, b) for i, b in enumerate(self.shown)])
        if self.shown:
            lst.highlighted = 0
            self._describe(self.shown[0])
        else:
            self.query_one("#add-info", Static).update("No matching bindings.")

    def _option(self, i: int, b: BindingInfo) -> Option:
        label = Text()
        label.append("✓ " if b.has_driver else "  ", style="green")
        label.append(b.compatible)
        if b.on_bus:
            label.append(f" [{b.on_bus}]", style="cyan")
        label.append(f"  {b.summary[:60]}", style="bright_black")
        return Option(label, id=str(i))

    def _describe(self, b: BindingInfo) -> None:
        text = Text()
        text.append(b.compatible + "\n", style="bold")
        text.append(f"{b.path.name}" + (f" · on {b.on_bus} bus" if b.on_bus else "")
                    + (" · provides " + "/".join(b.buses) + " bus" if b.buses else "")
                    + "\n", style="bright_black")
        text.append("driver found\n\n" if b.has_driver else "no driver found in the "
                    "workspace\n\n", style="green" if b.has_driver else "yellow")
        text.append(b.description.strip())
        self.query_one("#add-info", Static).update(text)

    @on(OptionList.OptionHighlighted, "#add-list")
    def highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if event.option and event.option.id is not None:
            self._describe(self.shown[int(event.option.id)])

    @on(Input.Submitted, "#add-filter")
    def submit_filter(self) -> None:
        lst = self.query_one("#add-list", OptionList)
        if lst.highlighted is not None and self.shown:
            self.dismiss(self.shown[lst.highlighted])

    def key_down(self) -> None:
        if self.focused is self.query_one("#add-filter"):
            self.query_one("#add-list").focus()

    @on(OptionList.OptionSelected, "#add-list")
    def picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.shown[int(event.option.id)])

    def action_toggle_fit(self) -> None:
        self.fit_only = not self.fit_only
        self.refill()

    def action_toggle_driver(self) -> None:
        self.driver_only = not self.driver_only
        self.refill()

    def action_cancel(self) -> None:
        self.dismiss(None)


class NodeFormScreen(ModalScreen[EditResult | None]):
    """Name, label, reg and required properties for a new node."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "apply", "Add", priority=True),
    ]

    # Written by the form itself, or with sensible defaults.
    SKIP = {"compatible", "reg", "status", "phandle"}

    def __init__(self, session: Session, parent_path: str, info: BindingInfo, base: Path):
        super().__init__()
        self.session, self.parent_path, self.info, self.base = session, parent_path, info, base
        edtlib = import_edtlib(session.ws.zephyr_base)
        m = session.model
        self.binding = edtlib.Binding(str(info.path), m.edt._binding_fname2path,
                                      require_description=False)
        parent = session.node(parent_path)
        self.addr_cells = parent._node.props["#address-cells"].to_num() \
            if "#address-cells" in parent._node.props else 2
        self.size_cells = parent._node.props["#size-cells"].to_num() \
            if "#size-cells" in parent._node.props else 1
        specs = self.binding.prop2specs
        self.has_reg = "reg" in specs
        self.reg_required = self.has_reg and specs["reg"].required
        self.required = [n for n, s in specs.items()
                         if s.required and n not in self.SKIP and s.type in values.EDITABLE]

    def _default_name(self) -> str:
        name = self.info.compatible.split(",")[-1]
        return name

    def _default_label(self, name: str) -> str:
        base = name.replace("-", "_").replace(".", "_")
        labels = self.session.model.labels
        if base not in labels:
            return base
        i = 0
        while f"{base}{i}" in labels:
            i += 1
        return f"{base}{i}"

    def compose(self) -> ComposeResult:
        s = self.session
        name = self._default_name()
        with VerticalScroll(id="form-dialog", classes="panel dialog") as dialog:
            dialog.border_title = f"New {self.info.compatible} under {self.parent_path}"
            dialog.border_subtitle = "ctrl+s / enter on the last field: add · esc: cancel"
            yield Static("Node name", classes="label")
            yield Input(value=name, id="form-name")
            yield Static("Label (for &references; empty for none)", classes="label")
            yield Input(value=self._default_label(name), id="form-label")
            if self.has_reg:
                cells = (f"{self.addr_cells} address cell(s)"
                         + (f" + {self.size_cells} size cell(s)" if self.size_cells else ""))
                req = "" if self.reg_required else " (optional)"
                yield Static(f"reg{req}: {cells}. The unit address (@…) is taken from it.",
                             classes="label")
                hint = {"i2c": "e.g. 0x76", "spi": "chip select, e.g. 0",
                        "i3c": "e.g. 0x48 0x0 0x0"}.get(self.info.on_bus or "", "e.g. 0x1000 0x100")
                yield Input(placeholder=hint, id="form-reg")
            for name in self.required:
                spec = self.binding.prop2specs[name]
                desc = " ".join((spec.description or "").split())
                yield Static(f"{name} ({spec.type}, required)"
                             + (f": {escape(desc[:120])}" if desc else ""), classes="label")
                if spec.type == "boolean":
                    yield Static("  (set to true)", classes="hint")
                elif spec.enum and spec.type in ("int", "string"):
                    yield Select([(str(v), str(v)) for v in spec.enum], id=prop_id(name),
                                 prompt="pick a value")
                else:
                    yield Input(id=prop_id(name), placeholder=spec.type)
            yield Static("Write to", classes="label")
            files = s.write_files(self.parent_path)
            yield Select(file_options(files, self.base, s.inputs.user_files),
                         value=s.default_write_file(self.parent_path), allow_blank=False,
                         id="form-file")
            yield Static("", id="form-error")

    def on_mount(self) -> None:
        if self.has_reg:
            self.query_one("#form-reg", Input).focus()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        inputs = list(self.query(Input))
        if event.input is inputs[-1]:
            self.action_apply()
        else:
            self.focus_next()

    def _error(self, msg: str) -> None:
        self.query_one("#form-error", Static).update(f"[red]{escape(msg)}[/red]")

    def action_apply(self) -> None:
        name = self.query_one("#form-name", Input).value.strip()
        label = self.query_one("#form-label", Input).value.strip() or None
        props: list[tuple[str, str | None]] = [("compatible", f'"{self.info.compatible}"')]
        if self.has_reg:
            raw = self.query_one("#form-reg", Input).value.strip()
            if raw:
                try:
                    reg = values.format_value("array", raw)
                except ValueError as exc:
                    self._error(f"reg: {exc}")
                    return
                cells = reg[1:-1].split()
                if len(cells) % (self.addr_cells + self.size_cells or 1):
                    self._error(f"reg needs {self.addr_cells + self.size_cells} cells per entry")
                    return
                if "@" not in name:
                    addr = cells[:self.addr_cells]
                    try:
                        unit = ",".join(f"{int(c, 0):x}" for c in addr)
                    except ValueError:
                        self._error("give the unit address in the name (name@addr) when reg "
                                    "uses macros")
                        return
                    name = f"{name}@{unit}"
                props.append(("reg", reg))
            elif self.reg_required:
                self._error("reg is required")
                return
        for pname in self.required:
            spec = self.binding.prop2specs[pname]
            if spec.type == "boolean":
                props.append((pname, None))
                continue
            widget = self.query_one(f"#{prop_id(pname)}")
            value = widget.value
            if not isinstance(value, str) or not value:
                self._error(f"{pname} is required")
                return
            try:
                props.append((pname, values.format_value(spec.type, str(value), spec.enum,
                                                         self.session.model.labels)))
            except ValueError as exc:
                self._error(f"{pname}: {exc}")
                return
        file = self.query_one("#form-file", Select).value
        result = self.session.add_node(self.parent_path, name, label, props, file)
        if result.ok:
            self.dismiss(result)
        else:
            self._error(result.message)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


DTS_CSS = """
    DtsView #main { layout: horizontal; height: 1fr; }
    DtsView #left { width: 45%; min-width: 40; }
    DtsView #right { width: 1fr; }
    DtsView #tree { border: none; background: $surface; padding: 0; }
    DtsView #filter { display: none; border: none; height: 1; padding: 0 1; }
    DtsView #filter.visible { display: block; }
    DtsView #details-panel { height: 45%; padding: 0 1; }
    DtsView #details-panel { height: 40%; }
    DtsView #settings-panel { height: 1fr; padding: 0 1; }
    DtsView #props { height: 1fr; border: none; background: $surface; padding: 0; }
    DtsView #prop-help { height: auto; max-height: 3; color: $text-muted;
                         border-top: solid $panel-lighten-2; }
    #pick-dialog { width: 100; height: auto; max-height: 80%; }
    #pick-input { margin-top: 1; }
    #pick-list { height: auto; max-height: 16; border: none; background: $surface; }
    #pick-error { height: auto; }
    DtsView #statusbar { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    .dialog { width: 100; max-width: 95%; height: auto; max-height: 90%; padding: 1 2; }
    ConfirmScreen, HelpScreen, FileScreen, TargetScreen, EditScreen, AddScreen,
    NodeFormScreen, ProblemsScreen, PickScreen { align: center middle; }
    #problems-dialog { width: 130; height: auto; max-height: 80%; }
    #problems-list { height: auto; max-height: 30; margin-top: 1; }
    #dts-confirm-dialog { width: 70; }
    #target-dialog, #add-dialog { height: 90%; }
    #add-dialog { width: 140; }
    #target-list, #file-list { height: 1fr; margin: 1 0; min-height: 5; }
    #file-list { height: auto; max-height: 20; }
    #target-extra { height: 3; }
    #target-extra Input { width: 1fr; }
    #add-body { height: 1fr; margin-top: 1; }
    #add-list { width: 60%; }
    #add-info-panel { width: 1fr; padding: 0 1; }
    .hint { color: $text-muted; }
    .label { margin-top: 1; }
    .sources { margin-top: 1; }
    #edit-error, #form-error { margin-top: 1; }
    #edit-complete { height: auto; max-height: 10; margin-top: 1;
                     border: round $panel-lighten-2; background: $surface; }
    #edit-complete:focus { border: round $success; }
    SpecifierEditor { height: auto; }
    .spec-row { height: auto; }
    .spec-ctrl { width: 45; }
    .spec-cell { width: 1fr; }

    DtsView.-portrait #main { layout: vertical; }
    DtsView.-portrait #left, DtsView.-portrait #right { width: 100%; min-width: 0; }
    DtsView.-portrait #left { height: 45%; }
    """


class DtsView(View):

    BINDINGS = [
        Binding("question_mark", "help", "Help"),
        Binding("slash", "filter", "Filter"),
        Binding("w", "pick_file", "Write to"),
        Binding("t", "pick_target", "Target"),
        Binding("p", "problems", "Problems"),
        Binding("u", "undo", "Undo"),
        Binding("r", "reload", "Reload"),
        Binding("z", "maximize", "Zoom", show=False),
    ]

    def __init__(self, config: AppConfig, **kwargs):
        super().__init__(**kwargs)
        self.config = config
        self.ws = config.workspace
        self.choice = config.choice
        self.session: Session | None = None
        self.bindings_catalog: list[BindingInfo] = []
        self.drivers: set[str] = set()
        self.chosen_names: set[str] = set()
        self.write_file: Path | None = None
        self.node_index: dict[str, TreeNode[str]] = {}
        self.problem_marks: dict[str, str] = {}  # node path -> worst severity
        self.more_open: set[str] = set()  # nodes showing all their settings
        self.setting_rows: dict[str, settings.Row] = {}
        self.last_row: dict[str, str] = {}  # node path -> highlighted setting
        self.filter_text = ""
        self.message = ""

    # -- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Panel(id="left", classes="panel") as left:
                left.border_title = "Devicetree"
                yield FilterInput(placeholder="filter: name, label or compatible", id="filter")
                tree = NodeTree("/", id="tree")
                tree.show_root = False
                yield tree
            with Vertical(id="right"):
                with ScrollPanel(id="details-panel", classes="panel") as details:
                    details.border_title = "Node"
                    yield Static("", id="details")
                with Panel(id="settings-panel", classes="panel") as panel:
                    panel.border_title = "Settings"
                    yield PropList(id="props")
                    yield Static("", id="prop-help")
        yield Static("", id="statusbar")

    @property
    def tree(self) -> NodeTree:
        return self.query_one("#tree", NodeTree)

    def on_resize(self, event) -> None:
        self.set_class(event.size.width < 110, "-portrait")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._poll_changes)
        if self.choice and not self.config.pick_target:
            self.open_target(self.choice)
        else:
            # Asked once the tab is shown (see activated()).
            self._ask_target = True

    def activated(self) -> None:
        if getattr(self, "_ask_target", False):
            self._ask_target = False
            self.action_pick_target()
        elif self.focused is None or self not in self.focused.ancestors:
            self.tree.focus()

    # -- target ----------------------------------------------------------

    def open_target(self, choice: TargetChoice) -> None:
        try:
            inputs = resolve_choice(self.ws, choice)
        except LookupError as exc:
            self.notify(str(exc), severity="error", timeout=8)
            self.action_pick_target()
            return
        session = Session(self.ws, inputs, self.config.work_dir)
        if not session.reload():
            self.notify(session.error or "parse failed", title="Devicetree does not parse",
                        severity="error", timeout=15)
            if self.session is None:
                self.action_pick_target()
            return
        self.choice = choice
        self.session = session
        save_choice(self.config.cwd, choice)
        self.write_file = session.model.default_write_file()
        self.query_one("#left").border_title = f"Devicetree · {inputs.description}"
        self.rebuild_tree()
        self.load_catalog()
        self.focus_here(self.tree)

    @work(thread=True, exclusive=True, group="catalog")
    def load_catalog(self) -> None:
        session = self.session
        roots = catalog.driver_roots(self.ws)
        drivers = catalog.driver_tokens(roots)
        bindings = catalog.scan_bindings(session.inputs.bindings_dirs, drivers)
        chosen = catalog.chosen_names(roots)
        self.call_from_thread(self._catalog_loaded, drivers, bindings, chosen)

    def _catalog_loaded(self, drivers: set[str], bindings: list[BindingInfo],
                        chosen: set[str]) -> None:
        self.drivers, self.bindings_catalog, self.chosen_names = drivers, bindings, chosen
        self.show_node()

    def action_pick_target(self) -> None:
        def done(choice: TargetChoice | None) -> None:
            if choice:
                self.open_target(choice)
            elif self.session is None:
                self.query_one("#details", Static).update(
                    "No target. Press t to pick a board or build directory.")
        self.push_screen(TargetScreen(self.ws, self.config.cwd, self.choice), done)

    # -- tree ------------------------------------------------------------

    def _node_label(self, node) -> Text:
        text = Text()
        if node.status == "okay":
            text.append("● ", style="green" if node._binding else "bright_black")
        else:
            text.append("○ ", style="bright_black")
        for label in node.labels:
            text.append(f"{label}: ", style="yellow")
        text.append(node.name, style="bold" if node.status == "okay" else "bright_black")
        if node.compats:
            text.append(f"  {node.compats[0]}", style="cyan" if node._binding else "red")
        if node.status != "okay":
            text.append(f"  {node.status}", style="bright_black italic")
        mark = self.problem_marks.get(node.path)
        if mark:
            text.append("  ✖" if mark == "error" else "  ⚠",
                        style="red" if mark == "error" else "yellow")
        return text

    def _matches(self, node) -> bool:
        words = self.filter_text.lower().split()
        hay = " ".join([node.path, *node.labels, *node.compats]).lower()
        return all(w in hay for w in words)

    def rebuild_tree(self, keep: str | None = None) -> None:
        if self.session is None or self.session.model is None:
            return
        tree = self.tree
        cursor = keep or (tree.cursor_node.data if tree.cursor_node else None)
        expanded = {p for p, n in self.node_index.items() if n.is_expanded}
        tree.clear()
        self.node_index = {}
        edt = self.session.model.edt
        self.problem_marks = {}
        for p in self.session.model.problems:
            for path in p.paths:
                if self.problem_marks.get(path) != "error":
                    self.problem_marks[path] = p.severity
        root = edt.get_node("/")
        visible = None
        if self.filter_text:
            visible = set()
            for n in edt.nodes:
                if self._matches(n):
                    p = n
                    while p is not None:
                        visible.add(p.path)
                        p = p.parent

        def add(parent: TreeNode[str], node) -> None:
            for child in node.children.values():
                if visible is not None and child.path not in visible:
                    continue
                item = parent.add(self._node_label(child), data=child.path,
                                  allow_expand=bool(child.children))
                self.node_index[child.path] = item
                add(item, child)

        top = tree.root.add(self._node_label(root), data="/", allow_expand=True)
        self.node_index["/"] = top
        add(top, root)
        top.expand()
        for path, item in self.node_index.items():
            if visible is not None or path in expanded:
                item.expand()
        if cursor:
            self.reveal(cursor)
        self.show_node()
        self.update_status()

    def reveal(self, path: str) -> None:
        """Move the cursor to `path` (or its nearest shown parent), unfolding parents."""
        while path not in self.node_index and path != "/":
            path = path.rsplit("/", 1)[0] or "/"
        item = self.node_index.get(path)
        if item:
            p = item.parent
            while p is not None:
                p.expand()
                p = p.parent
            self.call_after_refresh(self.tree.move_cursor, item)

    @property
    def current_path(self) -> str | None:
        node = self.tree.cursor_node
        return node.data if node else None

    @on(Tree.NodeHighlighted, "#tree")
    def _highlighted(self) -> None:
        self.show_node()

    def action_focus_settings(self) -> None:
        props = self.query_one("#props", PropList)
        props.focus()
        if props.highlighted is None and props.option_count:
            props.highlighted = 0

    def action_focus_tree(self) -> None:
        self.tree.focus()

    # -- details ---------------------------------------------------------

    def show_node(self) -> None:
        details = self.query_one("#details", Static)
        props = self.query_one("#props", PropList)
        s = self.session
        path = self.current_path
        node = s.node(path) if s and path else None
        props.clear_options()
        if node is None:
            details.update("")
            return
        base = self.ws.topdir
        t = Text()
        t.append(node.path + "\n", style="bold")
        if node.labels:
            t.append("labels: ", style="bright_black")
            t.append(", ".join(node.labels) + "\n", style="yellow")
        if node.compats:
            t.append("compatible: ", style="bright_black")
            t.append(", ".join(node.compats) + "\n", style="cyan")
        t.append("status: ", style="bright_black")
        t.append(node.status + "\n", style="green" if node.status == "okay" else "bright_black")
        if node._binding:
            t.append("binding: ", style="bright_black")
            t.append(rel(node.binding_path or "", base) + "\n")
            for compat in node.compats[:1]:
                if self.drivers:
                    has = catalog.compat_token(compat) in self.drivers
                    t.append("driver: ", style="bright_black")
                    t.append("found\n" if has else "none found in the workspace\n",
                             style="green" if has else "yellow")
        elif node.compats:
            t.append("no binding found for this compatible\n", style="red")
        if node.on_buses:
            t.append("on bus: ", style="bright_black")
            t.append(", ".join(node.on_buses) + "\n")
        if node.buses:
            t.append("provides bus: ", style="bright_black")
            t.append(", ".join(node.buses) + "\n")
        chosen = [k for k, v in s.model.edt.chosen_nodes.items() if v is node]
        if chosen:
            t.append("chosen: ", style="bright_black")
            t.append(", ".join(chosen) + "\n", style="magenta")
        if node.aliases:
            t.append("aliases: ", style="bright_black")
            t.append(", ".join(node.aliases) + "\n", style="magenta")
        for p in s.model.problems:
            if path in p.paths:
                t.append("\n")
                t.append_text(problem_text(p))
                t.append("\n")
        refs = [r for r in s.model.references.get(path, [])
                if r.source not in ("/chosen", "/aliases")]
        if refs:
            t.append(f"\nreferenced by ({len(refs)})\n", style="bold")
            for r in refs[:40]:
                src = s.node(r.source)
                name = f"&{src.labels[0]}" if src is not None and src.labels else r.source
                off = src is not None and not checks.enabled(src)
                t.append(f"  {name}", style="bright_black" if off else "yellow")
                t.append(f"  {r.prop}", style="bright_black" if off else "")
                if r.detail:
                    t.append(f"  {r.detail}", style="bright_black")
                if off:
                    t.append("  disabled", style="bright_black italic")
                t.append("\n")
            if len(refs) > 40:
                t.append(f"  … {len(refs) - 40} more\n", style="bright_black")
        sources = s.model.node_sources(path)
        if sources:
            t.append("\nsources\n", style="bold")
            writable = set(s.model.writable_files())
            for f, block in sources:
                style = "" if f in writable else "bright_black"
                t.append(f"  {rel(f, base)}:{block.line}", style=style)
                t.append(f"  {block.header}\n", style="bright_black")
        if node._binding and node.description:
            t.append("\n" + " ".join(node.description.split()), style="bright_black")
        details.update(t)
        self.fill_props(node)
        self._props_path = path
        self._focus_pending_prop()

    def fill_props(self, node) -> None:
        """The settings list for `node` (keeps the cursor on the same row)."""
        props = self.query_one("#props", PropList)
        keep = self.last_row.get(node.path)
        rows = settings.rows(self.session, node, node.path in self.more_open,
                             base_name=lambda f: rel(f, self.ws.topdir))
        self.setting_rows = {r.id: r for r in rows}
        props.clear_options()
        props.add_options([Option(r.text, id=r.id, disabled=r.kind == "head") for r in rows])
        ids = [r.id for r in rows]
        if keep in ids:
            props.highlighted = ids.index(keep)
        else:
            props.highlighted = next((i for i, r in enumerate(rows) if r.kind != "head"), None)
        self._show_help()

    @on(OptionList.OptionHighlighted, "#props")
    def _show_help(self, event=None) -> None:
        row = self._row()
        text = row.help if row else ""
        path = self.current_path
        if row and path and getattr(self, "_props_path", None) == path:
            self.last_row[path] = row.id
        if row and row.kind in ("prop", "status", "entry") and self.session and path:
            file = self._file_for(path, row.id if row.kind != "status" else None)
            if file:
                text += f"  ·  changes go to {rel(file, self.ws.topdir)}"
        self.query_one("#prop-help", Static).update(Text(text))

    # -- status ----------------------------------------------------------

    def update_status(self, message: str | None = None) -> None:
        if message is not None:
            self.message = message
        s = self.session
        parts = []
        if self.write_file:
            parts.append(f"write → {rel(self.write_file, self.ws.topdir)}")
        if s and s.undo_stack:
            parts.append(f"{len(s.undo_stack)} edit(s), u: undo")
        if self.filter_text:
            parts.append(f"filter: {self.filter_text}")
        if s and s.model and s.model.problems:
            n = len(s.model.problems)
            parts.append(f"{n} problem{'s' if n > 1 else ''}, p: list")
        if self.message:
            parts.append(self.message)
        self.query_one("#statusbar", Static).update("  ·  ".join(parts))

    def report(self, result: EditResult | None) -> None:
        if result is None:
            return
        if result.ok:
            self.rebuild_tree()
            self.update_status(result.message)
            self.notify(result.message, timeout=4)
        else:
            self.notify(result.message, severity="error", timeout=10)

    def _poll_changes(self) -> None:
        s = self.session
        if s and s.changed_on_disk():
            if s.reload():
                self.rebuild_tree()
                self.update_status("reloaded (files changed)")
            else:
                s._mtimes = s._snapshot()
                self.notify(s.error or "", title="Devicetree no longer parses",
                            severity="error", timeout=10)

    # -- actions ---------------------------------------------------------

    def _file_for(self, node_path: str, prop: str | None = None) -> Path | None:
        """The chosen write file if it can take this edit, else the smart default."""
        s = self.session
        files = s.write_files(node_path)
        if self.write_file in files and not prop:
            return self.write_file
        return s.default_write_file(node_path, prop)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_filter(self) -> None:
        f = self.query_one("#filter", FilterInput)
        f.add_class("visible")
        f.focus()

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self.rebuild_tree()

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self) -> None:
        self.tree.focus()

    def action_clear_filter(self) -> None:
        f = self.query_one("#filter", FilterInput)
        f.value = ""
        f.remove_class("visible")
        self.filter_text = ""
        self.rebuild_tree()
        self.tree.focus()

    def action_reload(self) -> None:
        if self.session and self.session.reload():
            self.rebuild_tree()
            self.update_status("reloaded")
        elif self.session:
            self.notify(self.session.error or "", severity="error", timeout=10)

    def action_problems(self) -> None:
        if self.session and self.session.model:
            self._show_problems()

    @work(exclusive=True, group="problems")
    async def _show_problems(self) -> None:
        # Waits until the dialog is gone, so focusing a property sticks.
        problem = await self.app.push_screen_wait(ProblemsScreen(self.session.model.problems))
        if problem is None:
            return
        if self.filter_text and problem.path not in self.node_index:
            self.action_clear_filter()
        self._pending_prop = (problem.path, problem.prop) if problem.prop else None
        self.reveal(problem.path)
        self.call_after_refresh(self._focus_pending_prop)

    def _focus_pending_prop(self) -> None:
        """Highlight the property a problem is about, once its node is shown."""
        pending = getattr(self, "_pending_prop", None)
        if not pending or getattr(self, "_props_path", None) != pending[0]:
            return  # the properties panel doesn't show that node yet
        self._pending_prop = None
        props = self.query_one("#props", PropList)
        for i in range(props.option_count):
            if props.get_option_at_index(i).id in (pending[1], f"ro:{pending[1]}"):
                props.highlighted = i
                props.focus()
                return

    def action_undo(self) -> None:
        if self.session:
            self.report(self.session.undo())

    def action_pick_file(self) -> None:
        s = self.session
        if not s:
            return
        files = s.model.writable_files()
        if not files:
            self.notify("no editable devicetree files for this target", severity="warning")
            return

        def done(path: Path | None) -> None:
            if path:
                self.write_file = path
                self.update_status()
        self.push_screen(FileScreen(files, self.write_file, self.ws.topdir,
                                    s.inputs.user_files), done)

    def action_add_node(self) -> None:
        s, path = self.session, self.current_path
        if not s or not path:
            return
        node = s.node(path)
        if not self.bindings_catalog:
            self.notify("still reading the bindings, try again in a moment")
            return
        if not s.write_files(path):
            self.notify("no editable file can add to this node", severity="warning")
            return

        def picked(info: BindingInfo | None) -> None:
            if info:
                self.push_screen(NodeFormScreen(s, path, info, self.ws.topdir), self.report)
        self.push_screen(AddScreen(self.bindings_catalog, path, list(node.buses)), picked)

    def action_toggle_status(self) -> None:
        s, path = self.session, self.current_path
        if not s or not path or path == "/":
            return
        node = s.node(path)
        if "status" not in s.editable_props(node):
            self.notify("this node has no binding; edit it by hand (o)", severity="warning")
            return
        file = self._file_for(path)
        if file is None:
            self.notify("no editable file for this node", severity="warning")
            return
        self.report(s.toggle_status(path, file))

    def action_delete_node(self) -> None:
        s, path = self.session, self.current_path
        if not s or not path:
            return
        file = self._file_for(path)
        plan = s.delete_plan(path, file) if file else None
        if plan is None:
            self.notify("can't delete this node here", severity="warning")
            return

        def confirmed(yes: bool) -> None:
            if yes:
                self.report(s.delete_node(path, file))
        self.push_screen(ConfirmScreen(f"Delete {path}?", plan[0]), confirmed)

    def _prop_name(self) -> str | None:
        props = self.query_one("#props", PropList)
        if props.highlighted is None:
            return None
        return props.get_option_at_index(props.highlighted).id

    def _row(self) -> settings.Row | None:
        name = self._prop_name()
        return self.setting_rows.get(name) if name else None

    def _set(self, path: str, name: str, text: str | None, delete: bool = False) -> EditResult:
        file = self._file_for(path, name)
        if file is None:
            return EditResult(False, "none of this target's files can be edited "
                                     "(add an app overlay)")
        return self.session.set_property(path, name, text, file, delete=delete)

    def action_toggle_prop(self) -> None:
        """space: switch the highlighted on/off setting, else change it."""
        row, path, s = self._row(), self.current_path, self.session
        if row is None or not s or not path:
            return
        if row.kind == "status":
            self.action_toggle_status()
        elif row.kind == "prop" and s.spec(s.node(path), row.id).type == "boolean":
            on = row.id in s.node(path)._node.props
            self.report(self._set(path, row.id, None, delete=on))
        else:
            self.action_edit_prop()

    def action_edit_prop(self) -> None:
        row, path, s = self._row(), self.current_path, self.session
        if row is None or not s or not path:
            return
        if row.kind in ("status",):
            self.action_toggle_status()
        elif row.kind == "more":
            self.more_open ^= {path}
            self.fill_props(s.node(path))
        elif row.kind == "child":
            self.reveal(row.id[5:])
        elif row.kind == "add":
            self.action_add_node()
        elif row.kind == "readonly":
            self.notify(f"{row.id[3:]} isn't in the node's binding, so it can't be changed "
                        "here; press o to open the file", severity="warning")
        elif row.kind == "entry":
            self._pick_label(path, row.id)
        elif row.kind == "new":
            self._pick_ref_name(path)
        elif row.kind == "prop":
            self._edit_setting(path, row.id)

    def _edit_setting(self, path: str, name: str) -> None:
        s = self.session
        node = s.node(path)
        spec = s.spec(node, name)
        type_ = spec.type
        if type_ == "boolean":
            on = name in node._node.props
            self.report(self._set(path, name, None, delete=on))
            return
        file = self._file_for(path, name)
        if file is None:
            self.notify("none of this target's files can be edited (add an app overlay)",
                        severity="warning")
            return
        if type_ == "phandle-array":
            self.push_screen(EditScreen(s, path, name, file), self.report)
            return
        raw = settings.source_text(s, path, name)
        current = values.to_input(type_, raw) if raw is not None else ""
        title = settings.friendly(name)
        desc = " ".join((spec.description or "").split())
        apply = lambda v: self._set(path, name, v)  # noqa: E731
        if spec.enum:
            default = spec.default

            def enum_choices(token: str) -> list:
                out = []
                for v in spec.enum:
                    if token.lower() in str(v).lower():
                        t = Text(str(v), style="bold")
                        if v == default:
                            t.append("  default", style="bright_black italic")
                        out.append((str(v), t))
                return out
            self.push_screen(PickScreen(title, desc, enum_choices, apply,
                                        current or ("" if default is None else str(default)),
                                        free=False, placeholder="type to search"),
                             self.report)
        elif type_ in settings.LABEL_TYPES:
            self.push_screen(PickScreen(title, desc, lambda t: settings.label_choices(s, t),
                                        apply, current, free=False,
                                        placeholder="type to search for a node"), self.report)
        elif type_ == "phandles":
            self.push_screen(PickScreen(title, desc, lambda t: settings.label_choices(s, t),
                                        apply, current, word=True,
                                        placeholder="&node1 &node2"), self.report)
        elif type_ in ("int", "array", "compound"):
            context = macros.context_words(*node.compats[:1])
            primary = macros.context_words(name, *node.buses, *node.on_buses)

            def macro_choices(token: str) -> list:
                if token[:1].isdigit():
                    return []
                out = []
                is_hz = settings._is_hz(name)
                for m in macros.rank(s.model.macros, token, context | primary, primary=primary):
                    ins, t = macro_choice(m)
                    v = macros.value_of(m)
                    if is_hz and not token and (v is None or v < 1000):
                        continue  # not a frequency (channel numbers and such)
                    if v is not None and is_hz:
                        t.append(f"  {settings.hz(v)}", style="green")
                    out.append((ins, t))
                return out
            hint = {"int": "a number or a macro", "array": "numbers, e.g. 1 2 3"}.get(
                type_, "a value")
            self.push_screen(PickScreen(title, desc, macro_choices, apply, current, word=True,
                                        placeholder=hint), self.report)
        else:
            hint = {"string": "text", "string-array": "texts, separated by commas",
                    "uint8-array": "hex bytes, e.g. 00 1f a0"}.get(type_, "a value")
            self.push_screen(PickScreen(title, desc, lambda t: [], apply, current,
                                        placeholder=hint), self.report)

    def _pick_label(self, path: str, name: str) -> None:
        s = self.session
        raw = settings.source_text(s, path, name)
        self.push_screen(PickScreen(name, "Which node should this point at?",
                                    lambda t: settings.label_choices(s, t),
                                    lambda v: self._set(path, name, v), raw or "", free=False,
                                    placeholder="type to search for a node"), self.report)

    def _pick_ref_name(self, path: str) -> None:
        s = self.session
        chosen = path == "/chosen"
        pattern = REF_NODES[path]
        used = set(s.node(path)._node.props)

        def named(name: str):
            if not pattern.match(name):
                return EditResult(False, "use lowercase letters, digits and - (and , . _ "
                                         "for chosen)" if chosen else
                                         "use lowercase letters, digits and -")
            return EditResult(True, name)

        def then(result) -> None:
            if result is not None:
                self._pick_label(path, result.message)
        choices = (lambda t: settings.chosen_choices(self.chosen_names, used, t)) if chosen \
            else (lambda t: [])
        self.push_screen(PickScreen("New chosen entry" if chosen else "New alias",
                                    "First the name, then the node it points at.", choices,
                                    named, placeholder="e.g. zephyr,console" if chosen
                                    else "e.g. led0"), then)

    def action_delete_prop(self) -> None:
        row, path, s = self._row(), self.current_path, self.session
        if row is None or not s or not path or row.kind not in ("prop", "entry"):
            return
        if s.effective(path, row.id) is None:
            self.notify(f"{settings.friendly(row.id)} is not set")
            return
        result = self._set(path, row.id, None, delete=True)
        if result.ok:
            result.message += "  (u: undo)"
        self.report(result)

    def action_open_editor(self) -> None:
        s, path = self.session, self.current_path
        if not s or not path:
            return
        target = None
        if self.focused is self.query_one("#props"):
            name = self._prop_name()
            if name:
                target = s.effective(path, name.removeprefix("ro:"))
        if target is None:
            sources = s.model.node_sources(path)
            writable = [x for x in sources if x[0] in s.model.writable_files()]
            pick = (writable or sources or [None])[-1]
            if pick:
                target = (pick[0], pick[1].line)
        if target is None:
            node = s.node(path)
            target = (Path(node._node.filename), node._node.lineno)
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
        with self.suspend():
            subprocess.run([*editor, f"+{target[1]}", str(target[0])])
        self.action_reload()

    def action_maximize(self) -> None:
        if self.screen.maximized:
            self.screen.minimize()
            return
        widget = self.focused
        while widget is not None and not getattr(widget, "ALLOW_MAXIMIZE", False):
            widget = widget.parent
        if widget is not None:
            self.screen.maximize(widget)


class LazyDtsApp(App):
    """The devicetree tab on its own (used by the tests)."""

    CSS = COMMON_CSS + DTS_CSS
    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, config: AppConfig):
        super().__init__()
        self.view = DtsView(config, id="dts")

    def compose(self) -> ComposeResult:
        yield self.view
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self.view.activated)

    @property
    def tree(self):
        return self.view.tree

    def __getattr__(self, name: str):
        view = self.__dict__.get("view")
        if view is not None and not name.startswith("_"):
            return getattr(view, name)
        raise AttributeError(name)
