"""The devicetree tab: browse and edit a target's devicetree."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

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

from . import catalog, checks, macros, specifiers, values
from .catalog import BindingInfo
from .editors import CompleteInput, Completer, SpecifierEditor, SpecRow, macro_choice
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
    BINDINGS = [
        Binding("enter,e", "edit_prop", "Edit"),
        Binding("d", "delete_prop", "Remove"),
        Binding("o", "open_editor", "Open"),
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
  j/k, h/l, arrows   move, fold
  /                  filter by name, label or compatible
  a                  add a node under the selected node
  s                  toggle status okay / disabled
  d                  delete the node
  o                  open the source in $EDITOR
  tab                go to the properties

[b]Properties[/b]
  enter, e           edit (only properties from the node's binding)
  d                  remove the value (falls back to the earlier one)
  o                  open where the value is set

[b]Anywhere[/b]
  p                  problems: pins used twice, two devices at one address,
                     overlapping partitions, enabled nodes needing disabled ones
  w                  pick the file edits go to
  t                  pick another target
  u                  undo the last edit
  r                  reload
  z                  maximize the focused panel
  q                  quit

Edits are written straight to the source file, then the devicetree is parsed
again. If it no longer parses, the file is put back and the error is shown.
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


# Types whose values name other nodes, and types whose cells can use macros.
LABEL_TYPES = {"phandle", "phandles", "path"}
MACRO_TYPES = {"int", "array", "phandle-array", "compound"}


class EditScreen(ModalScreen[EditResult | None]):
    """Edit one property, limited to what its binding allows."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "apply", "Apply", priority=True),
        Binding("ctrl+n", "add_entry", "Add entry", priority=True),
        Binding("ctrl+r", "remove_entry", "Remove entry", priority=True),
        Binding("ctrl+t", "toggle_text", "Type as text", priority=True),
    ]

    def __init__(self, session: Session, node_path: str, name: str | None, base: Path,
                 chosen_names: set[str] | None = None):
        """name None: a new /chosen or /aliases entry, named in the dialog."""
        super().__init__()
        self.session, self.node_path, self.prop, self.base = session, node_path, name, base
        self.chosen_names = chosen_names or set()
        node = session.node(node_path)
        if node_path in REF_NODES:
            self.spec = SimpleNamespace(type="path", description=self._ref_description(),
                                        required=False, enum=None, const=None, default=None,
                                        deprecated=False)
        else:
            self.spec = session.spec(node, name)
        self.space: str | None = None
        self.ctrls: list[specifiers.Controller] = []
        if self.spec.type == "phandle-array":
            self.space = specifiers.space(name, self.spec)
            self.ctrls = specifiers.controllers(session.model.edt, self.space)

    def compose(self) -> ComposeResult:
        s, spec, m = self.session, self.spec, self.session.model
        node = s.node(self.node_path)
        with VerticalScroll(id="edit-dialog", classes="panel dialog") as dialog:
            what = "chosen" if self.node_path == "/chosen" else "alias"
            dialog.border_title = f"{self.prop or 'new ' + what}  ({spec.type})"
            yield Static(self._info(node), classes="hint")
            if self.prop is None:
                yield Static("Name", classes="label")
                yield CompleteInput(placeholder="e.g. zephyr,console" if what == "chosen"
                                    else "e.g. led0", id="edit-name", kind="name", whole=True)
            sources = m.prop_sources(self.node_path, self.prop) if self.prop else []
            eff = s.effective(self.node_path, self.prop) if self.prop else None
            if sources:
                lines = []
                for f, stmt in sources:
                    mark = "→" if eff == (f, stmt.line) else " "
                    text = (f"/delete-property/" if stmt.kind == "delete-property"
                            else (f"= {stmt.value}" if stmt.value is not None else "(set)"))
                    lines.append(f"{mark} {escape(rel(f, self.base))}:{stmt.line}  "
                                 f"{escape(text)}")
                yield Static("[b]Set in[/b]\n" + "\n".join(lines), classes="sources")
            current = self._current_raw(sources, eff)
            if spec.type == "boolean":
                yield Select([("true", "true"), ("false (remove)", "false")],
                             value="true" if node.props.get(self.prop)
                             and node.props[self.prop].val else "false",
                             allow_blank=False, id="edit-value")
            elif spec.enum and spec.type in ("int", "string"):
                opts = [(str(v), str(v)) for v in spec.enum]
                cur = values.to_input(spec.type, current) if current else None
                if cur is None and spec.default is not None:
                    cur = str(spec.default)
                yield Select(opts, value=cur if cur in [o[1] for o in opts] else Select.BLANK,
                             id="edit-value", prompt="pick a value")
            else:
                if self.prop is None:
                    yield Static("Value", classes="label")
                yield self._value_widget(current, structured=True)
            yield Completer(self._choices, id="edit-complete")
            files = s.write_files(self.node_path)
            default = s.default_write_file(self.node_path, self.prop)
            yield Static("Write to", classes="label")
            yield Select(file_options(files, self.base, s.inputs.user_files), value=default,
                         allow_blank=False, id="edit-file")
            yield Static("", id="edit-error")

    def on_mount(self) -> None:
        self._update_keys()
        if self.prop is None:
            self.query_one("#edit-name").focus()

    def _value_widget(self, text: str | None, structured: bool):
        type_ = self.spec.type
        if type_ == "phandle-array" and structured and self.ctrls:
            entries = specifiers.parse(text, self.ctrls)
            if entries is not None:
                return SpecifierEditor(self.ctrls, entries, self._macro_context(),
                                       id="edit-value")
        value = values.to_input(type_, text) if text is not None else ""
        if type_ in LABEL_TYPES or type_ in MACRO_TYPES:
            return CompleteInput(value=value, placeholder=self._placeholder(), id="edit-value",
                                 kind="label" if type_ in LABEL_TYPES else "macro",
                                 context=self._macro_context())
        return Input(value=value, placeholder=self._placeholder(), id="edit-value")

    def _ref_description(self) -> str:
        if self.node_path == "/chosen":
            what = (f"DT_CHOSEN({self.prop.replace(',', '_').replace('-', '_')})"
                    if self.prop else "DT_CHOSEN()")
            return f"The node Zephyr uses for this purpose; code reads it with {what}."
        what = f"DT_ALIAS({self.prop.replace('-', '_')})" if self.prop else "DT_ALIAS()"
        return f"Another name for a node; code reads it with {what}."

    def _macro_context(self) -> set[str]:
        node = self.session.node(self.node_path)
        return macros.context_words(self.prop or "", self.space, *node.compats[:1], *node.buses,
                                    *node.on_buses)

    def _update_keys(self) -> None:
        keys = ["enter: apply"]
        widget = self.query_one("#edit-value")
        if isinstance(widget, SpecifierEditor):
            keys += ["ctrl+n/r: add/remove entry", "ctrl+t: as text"]
        elif self.spec.type == "phandle-array" and self.ctrls:
            keys.append("ctrl+t: fields")
        if isinstance(widget, CompleteInput) or isinstance(widget, SpecifierEditor):
            keys.append("↓: choices")
        keys.append("esc: cancel")
        self.query_one("#edit-dialog").border_subtitle = " · ".join(keys)

    def _choices(self, token: str, target: CompleteInput) -> list:
        m = self.session.model
        if target.kind == "label":
            return self._label_choices(token)
        if target.kind == "name":
            return self._name_choices(token)
        if token[:1].isdigit():
            return []
        return [macro_choice(x) for x in macros.rank(m.macros, token, target.context)]

    def _label_choices(self, token: str) -> list:
        edt = self.session.model.edt
        labels = edt._dt.label2node
        want = token.lstrip("&").lower()
        if want and want in {k.lower() for k in labels}:
            want = ""  # a complete label: offer all of them, to change it
        out = []
        for label in sorted(labels, key=str.lower):
            low = label.lower()
            if want and want not in low:
                continue
            node = self.session.node(labels[label].path)
            t = Text()
            t.append(f"&{label}", style="yellow")
            t.append(f"  {labels[label].path}", style="bright_black")
            if node is not None and node.compats:
                t.append(f"  {node.compats[0]}", style="cyan")
            if node is not None and node.status != "okay":
                t.append(f"  {node.status}", style="bright_black italic")
            has_compat = node is not None and bool(node.compats)
            out.append((not low.startswith(want), not has_compat, f"&{label}", t))
        out.sort(key=lambda c: c[:2])
        return [(ins, t) for *_, ins, t in out]

    def _name_choices(self, token: str) -> list:
        if self.node_path != "/chosen":
            return []
        node = self.session.node(self.node_path)
        used = set(node._node.props) if node else set()
        want = token.lower()
        out = []
        for name in sorted(self.chosen_names | used):
            if want and want not in name:
                continue
            t = Text(name, style="bold" if name not in used else "bright_black")
            if name in used:
                t.append("  already set", style="bright_black italic")
            out.append((name in used, not name.startswith(want), name, t))
        out.sort(key=lambda c: c[:2])
        return [(name, t) for *_, name, t in out]

    def _current_raw(self, sources, eff) -> str | None:
        for f, stmt in reversed(sources):
            if eff == (f, stmt.line) and stmt.kind == "set":
                return stmt.value
        return None

    def _placeholder(self) -> str:
        return {
            "int": "e.g. 100, 0x20 or MACRO(1)",
            "array": "e.g. 1 2 3",
            "uint8-array": "hex bytes, e.g. 00 1f a0",
            "string": "text (no quotes needed)",
            "string-array": "comma separated, e.g. a, b",
            "phandle": "node label, e.g. &gpio0",
            "phandles": "node labels, e.g. &dma0 &dma1",
            "phandle-array": "e.g. <&gpio0 5 GPIO_ACTIVE_LOW>",
            "path": "&label or /path/to/node",
        }.get(self.spec.type, "value")

    def _info(self, node) -> str:
        spec = self.spec
        parts = []
        if spec.description:
            parts.append(escape(" ".join(spec.description.split())))
        facts = []
        if spec.required:
            facts.append("[red]required[/red]")
        if spec.enum:
            facts.append("one of: " + escape(", ".join(map(str, spec.enum))))
        if spec.const is not None:
            facts.append(f"const: {escape(str(spec.const))}")
        if spec.default is not None:
            facts.append(f"default: {escape(str(spec.default))}")
        if spec.deprecated:
            facts.append("[yellow]deprecated[/yellow]")
        if self.space is not None:
            facts.append(self._cells_hint())
        if facts:
            parts.append(" · ".join(facts))
        return "\n".join(parts) or "(no description in the binding)"

    def _cells_hint(self) -> str:
        """`<&controller pin flags>` from the controllers in the tree."""
        if not self.ctrls:
            return escape(f"no node in the tree has #{self.space}-cells")
        cells: dict[tuple[str, ...], list[str]] = {}
        for c in self.ctrls:
            cells.setdefault(tuple(c.cells), []).append(c.ref)
        hints = []
        for names, refs in list(cells.items())[:3]:
            shown = ", ".join(refs[:4]) + (", …" if len(refs) > 4 else "")
            hints.append(f"<&ctrl {' '.join(names)}> for {shown}")
        return escape("; ".join(hints))

    # -- events ----------------------------------------------------------

    @property
    def completer(self) -> Completer:
        return self.query_one("#edit-complete", Completer)

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if event.input.id == "edit-name":
            self.query_one("#edit-value").focus()
        else:
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
            self._error("can't remove the only entry; remove the property instead (d)")

    async def action_toggle_text(self) -> None:
        widget = self.query_one("#edit-value")
        if self.spec.type != "phandle-array" or not self.ctrls:
            return
        if isinstance(widget, SpecifierEditor):
            text = specifiers.compose([e for e in widget.entries() if e[0]])
            new = self._value_widget(text, structured=False)
        else:
            text = widget.value.strip()
            if text and "<" not in text:
                text = f"<{text}>"
            if specifiers.parse(text, self.ctrls) is None:
                self._error("can't split this into controllers and cells; edit it as text")
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
        file = self.query_one("#edit-file", Select).value
        name = self.prop
        if name is None:
            name = self.query_one("#edit-name", Input).value.strip()
            if not name:
                self._error("give it a name")
                return
        if self.spec.type == "boolean":
            delete = widget.value == "false"
            result = self.session.set_property(self.node_path, name, None, file,
                                               delete=delete)
        elif isinstance(widget, SpecifierEditor):
            entries = widget.entries()
            if err := specifiers.check(entries, self.ctrls):
                self._error(err)
                return
            result = self.session.set_property(self.node_path, name,
                                               specifiers.compose(entries), file)
        else:
            value = widget.value
            if not isinstance(value, str):
                self._error("pick a value")
                return
            result = self.session.set_property(self.node_path, name, str(value), file)
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
                    yield Select([(str(v), str(v)) for v in spec.enum], id=f"prop-{name}",
                                 prompt="pick a value")
                else:
                    yield Input(id=f"prop-{name}", placeholder=spec.type)
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
            widget = self.query_one(f"#prop-{pname}")
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
    DtsView #props { height: 1fr; border: round $panel-lighten-2; background: $surface; }
    DtsView #props:focus { border: round $success; }
    DtsView #statusbar { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    .dialog { width: 100; max-width: 95%; height: auto; max-height: 90%; padding: 1 2; }
    ConfirmScreen, HelpScreen, FileScreen, TargetScreen, EditScreen, AddScreen,
    NodeFormScreen, ProblemsScreen { align: center middle; }
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
                props = PropList(id="props")
                props.border_title = "Properties"
                yield props
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
        self.tree.focus()

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

    @on(Tree.NodeSelected, "#tree")
    def _selected(self, event) -> None:
        if not event.node.allow_expand:
            self.query_one("#props").focus()

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

    def fill_ref_props(self, node) -> None:
        """/chosen and /aliases: each entry points at a node."""
        props = self.query_one("#props", PropList)
        names = list(node._node.props)
        width = min(max([len(n) for n in names] or [10]), 32)
        rows = []
        for name, prop in node._node.props.items():
            t = Text()
            t.append(name.ljust(width), style="bold")
            t.append("  ")
            try:
                target = prop.to_path()
                labels = target.labels
                t.append(f"&{labels[0]}" if labels else target.path, style="yellow")
            except Exception:  # DTError: not a path or reference
                t.append(raw_value(prop))
            eff = self.session.effective(node.path, name)
            if eff:
                t.append(f"  {eff[0].name}:{eff[1]}", style="bright_black")
            rows.append(Option(t, id=name))
        what = "chosen" if node.path == "/chosen" else "alias"
        rows.append(Option(Text(f"+ add {what}…", style="green"), id="new:"))
        props.add_options(rows)
        props.border_title = "Chosen" if node.path == "/chosen" else "Aliases"

    def fill_props(self, node) -> None:
        s = self.session
        props = self.query_one("#props", PropList)
        if node.path in REF_NODES:
            self.fill_ref_props(node)
            return
        editable = s.editable_props(node)
        rows = []
        width = max([len(n) for n in [*editable, *node._node.props]] or [10])
        width = min(width, 32)
        shown = set()
        for name in editable:
            shown.add(name)
            spec = s.spec(node, name)
            t = Text()
            t.append(name.ljust(width), style="bold")
            t.append("  ")
            if name in node._node.props:
                prop = node.props.get(name)
                t.append(values.show(prop.val) if prop else "set")
                eff = s.effective(node.path, name)
                if eff:
                    t.append(f"  {eff[0].name}:{eff[1]}", style="bright_black")
            elif spec.required:
                t.append("required, not set", style="red")
            elif spec.default is not None:
                t.append(f"default {spec.default}", style="bright_black")
            else:
                t.append("-", style="bright_black")
            rows.append(Option(t, id=name))
        for name, prop in node._node.props.items():
            if name in shown:
                continue
            t = Text()
            t.append(name.ljust(width), style="bright_black")
            t.append("  ")
            edt_prop = node.props.get(name)
            t.append(values.show(edt_prop.val) if edt_prop else raw_value(prop),
                     style="bright_black")
            t.append("  read-only", style="bright_black italic")
            rows.append(Option(t, id=f"ro:{name}"))
        props.add_options(rows)
        props.border_title = "Properties" if node._binding else \
            "Properties (no binding: read-only)"

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

    def action_edit_prop(self) -> None:
        s, path, name = self.session, self.current_path, self._prop_name()
        if not s or not path or not name:
            return
        if name.startswith("ro:"):
            self.notify(f"{name[3:]} is not in the node's binding; edit it by hand (o)",
                        severity="warning")
            return
        if not s.write_files(path):
            self.notify("no editable file for this node", severity="warning")
            return
        self.push_screen(EditScreen(s, path, None if name == "new:" else name, self.ws.topdir,
                                    self.chosen_names), self.report)

    def action_delete_prop(self) -> None:
        s, path, name = self.session, self.current_path, self._prop_name()
        if not s or not path or not name or name.startswith("ro:") or name == "new:":
            return
        if s.effective(path, name) is None:
            self.notify(f"{name} is not set")
            return
        file = s.default_write_file(path, name)
        if file is None:
            self.notify("no editable file for this node", severity="warning")
            return
        eff = s.effective(path, name)
        if eff and eff[0] == file:
            msg = (f"Remove {name} from {rel(file, self.ws.topdir)}:{eff[1]}. "
                   "An earlier value, if any, applies again.")
        else:
            msg = f"Add /delete-property/ {name}; to {rel(file, self.ws.topdir)}."

        def confirmed(yes: bool) -> None:
            if yes:
                self.report(s.set_property(path, name, None, file, delete=True))
        self.push_screen(ConfirmScreen(f"Remove {name}?", msg), confirmed)

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
