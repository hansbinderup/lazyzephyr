"""The Kconfig tab: browse a build's options, see why each has its value, and
change them in the application's conf files."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, OptionList, Static, Tree
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from ..dts.app import ConfirmScreen
from ..dts.editors import PickScreen
from ..dts.workspace import Workspace, find_build_dirs
from ..view import COMMON_CSS, ForwardActions, View
from . import describe, loader
from .session import Problem, Result, Session


@dataclass
class AppConfig:
    cwd: Path
    workspace: Workspace
    build_dir: Path | None = None


# --------------------------------------------------------------------------
# Remember the build per directory
# --------------------------------------------------------------------------


def _state_file() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "lazyzephyr" / "kconfig.json"


def load_build(cwd: Path) -> Path | None:
    try:
        raw = json.loads(_state_file().read_text()).get(str(cwd))
    except (OSError, ValueError, AttributeError):
        return None
    return Path(raw) if isinstance(raw, str) and Path(raw).is_dir() else None


def save_build(cwd: Path, build: Path) -> None:
    path = _state_file()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = {}
    data[str(cwd)] = str(build)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------


class KTree(ForwardActions, Tree):
    BINDINGS = [
        Binding("enter", "change", "Change"),
        Binding("space", "switch", "On/off"),
        Binding("d", "remove", "Remove"),
        Binding("backspace", "back", "Back", show=False),
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


class ResultList(ForwardActions, OptionList):
    BINDINGS = [
        Binding("enter", "change", "Change"),
        Binding("space", "switch", "On/off"),
        Binding("d", "remove", "Remove"),
        Binding("escape", "clear_search", "Close search"),
        Binding("backspace", "back", "Back", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]


class RelatedList(ForwardActions, OptionList):
    BINDINGS = [
        Binding("enter", "jump", "Go to"),
        Binding("space", "switch_related", "On/off"),
        Binding("escape,h,left", "focus_main", "Back"),
        Binding("backspace", "back", "Previous", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]


class SearchInput(ForwardActions, Input):
    BINDINGS = [Binding("escape", "clear_search", "Close search")]


class Panel(Vertical):
    ALLOW_MAXIMIZE = True


class ScrollPanel(VerticalScroll):
    ALLOW_MAXIMIZE = True


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------


class BuildScreen(ModalScreen[Path | None]):
    """Pick the build directory whose Kconfig to show."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, ws: Workspace, cwd: Path, current: Path | None):
        super().__init__()
        self.ws, self.cwd, self.current = ws, cwd, current
        self.builds: list[Path] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="kbuild-dialog", classes="panel dialog") as dialog:
            dialog.border_title = "Which build?"
            dialog.border_subtitle = "enter: open · esc: cancel"
            yield Static("Kconfig is read from a build directory (build in the Build tab "
                         "first). Changes go to the application's conf files.",
                         classes="hint")
            yield OptionList(id="kbuild-list")

    def on_mount(self) -> None:
        self.query_one(OptionList).loading = True
        self.scan()

    @work(thread=True)
    def scan(self) -> None:
        builds = find_build_dirs(self.ws, self.cwd)
        self.app.call_from_thread(self._found, builds)

    def _found(self, builds: list[Path]) -> None:
        lst = self.query_one(OptionList)
        lst.loading = False
        self.builds = builds
        if not builds:
            lst.add_option(Option(Text("No build directories found. Build first.",
                                       style="bright_black"), disabled=True))
            return
        for b in builds:
            try:
                label = str(b.relative_to(self.ws.topdir))
            except ValueError:
                label = str(b)
            lst.add_option(Option(label))
        if self.current in builds:
            lst.highlighted = builds.index(self.current)
        lst.focus()

    @on(OptionList.OptionSelected, "#kbuild-list")
    def picked(self, event: OptionList.OptionSelected) -> None:
        if self.builds:
            self.dismiss(self.builds[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class KProblemsScreen(ModalScreen[Problem | None]):
    BINDINGS = [Binding("escape,q,p", "cancel", "Close")]

    def __init__(self, problems: list[Problem]):
        super().__init__()
        self.problems = problems

    def compose(self) -> ComposeResult:
        with Vertical(id="kproblems-dialog", classes="panel dialog") as dialog:
            dialog.border_title = f"Problems in your conf files ({len(self.problems)})"
            dialog.border_subtitle = "enter: go to the option · esc: close"
            if not self.problems:
                yield Static("Every line in your conf files takes effect.", classes="hint")
                return
            opts = []
            for p in self.problems:
                t = Text("✖ " if p.severity == "error" else "⚠ ",
                         style="red" if p.severity == "error" else "yellow")
                t.append(p.message)
                opts.append(Option(t))
            yield OptionList(*opts, id="kproblems-list")

    @on(OptionList.OptionSelected, "#kproblems-list")
    def picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.problems[event.option_index])

    def action_cancel(self) -> None:
        self.dismiss(None)


class KHelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "close", "Close")]
    TEXT = """\
[b]Options[/b] (the tree, or search results)
  j/k, arrows        move;  l/h unfold / fold
  space              switch an option on / off
  enter              change it: on/off, pick from a list, or type a value
  d                  remove it from your conf file (its default applies again)
  /                  search every option by name, prompt or help
  A                  show hidden options too (those whose dependencies aren't met)
  tab                go to the related options
  o                  open where it is set (or defined) in $EDITOR

[b]Related options[/b]
  enter              go to that option;  space switches it on / off
  esc / backspace    back

[b]Anywhere[/b]
  p                  problems: lines in your conf files that have no effect
  t                  pick another build
  u                  undo the last change
  r                  reload

Changes go into your prj.conf (or the conf file that already sets the option), never
into .config. The values shown are what the next build will use; "not built yet"
marks options that differ from the last build. If an option needs others turned on
first, you are asked whether to turn them on too.
"""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="khelp-dialog", classes="panel dialog") as dialog:
            dialog.border_title = "Kconfig help"
            yield Static(self.TEXT)

    def action_close(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# The view
# --------------------------------------------------------------------------


KCONFIG_CSS = """
    KconfigView #kmain { layout: horizontal; height: 1fr; }
    KconfigView #kleft { width: 50%; min-width: 40; }
    KconfigView #kright { width: 1fr; }
    KconfigView #ktree { border: none; background: $surface; padding: 0; }
    KconfigView #ksearch { display: none; border: none; height: 1; padding: 0 1; }
    KconfigView #ksearch.visible { display: block; }
    KconfigView #kresults { display: none; border: none; background: $surface; height: 1fr; }
    KconfigView #kresults.visible { display: block; }
    KconfigView #kdetails-panel { height: 65%; padding: 0 1; }
    KconfigView #krelated-panel { height: 1fr; padding: 0 1; }
    KconfigView #krelated { border: none; background: $surface; height: 1fr; }
    KconfigView #kstatus { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    KconfigView.-portrait #kmain { layout: vertical; }
    KconfigView.-portrait #kleft, KconfigView.-portrait #kright { width: 100%; min-width: 0; }
    KconfigView.-portrait #kleft { height: 45%; }
    BuildScreen, KProblemsScreen, KHelpScreen { align: center middle; }
    #kbuild-dialog, #kproblems-dialog { width: 110; height: auto; max-height: 80%; }
    #kbuild-list, #kproblems-list { height: auto; max-height: 24; margin-top: 1; }
    #khelp-dialog { width: 96; height: auto; max-height: 90%; }
"""


class KconfigView(View):
    BINDINGS = [
        Binding("question_mark", "help", "Help"),
        Binding("slash", "search", "Search"),
        Binding("A", "toggle_all", "Show hidden"),
        Binding("p", "problems", "Problems"),
        Binding("t", "pick_build", "Build dir"),
        Binding("u", "undo", "Undo"),
        Binding("r", "reload", "Reload"),
        Binding("o", "open_editor", "Open file", show=False),
        Binding("z", "maximize", "Zoom", show=False),
    ]

    def __init__(self, config: AppConfig, **kwargs):
        super().__init__(**kwargs)
        self.config = config
        self.ws = config.workspace
        self.session: Session | None = None
        self.show_all = False
        self.current = None  # Symbol, Choice or MenuNode shown on the right
        self.history: list = []
        self.items: dict[int, TreeNode] = {}  # id(MenuNode) -> tree node
        self.populated: set[int] = set()
        self.results: list = []
        self.related_rows: list[str] = []
        self.message = ""
        self._asked = False

    def compose(self) -> ComposeResult:
        with Horizontal(id="kmain"):
            with Panel(id="kleft", classes="panel") as left:
                left.border_title = "Kconfig"
                yield SearchInput(placeholder="search: name, prompt or help", id="ksearch")
                tree = KTree("Kconfig", id="ktree")
                tree.show_root = False
                yield tree
                yield ResultList(id="kresults")
            with Vertical(id="kright"):
                with ScrollPanel(id="kdetails-panel", classes="panel") as details:
                    details.border_title = "Option"
                    yield Static("", id="kdetails")
                with Panel(id="krelated-panel", classes="panel") as rel:
                    rel.border_title = "Related"
                    yield RelatedList(id="krelated")
        yield Static("", id="kstatus")

    @property
    def tree(self) -> KTree:
        return self.query_one("#ktree", KTree)

    @property
    def lib(self):
        return self.session.model.kconfiglib

    def on_resize(self, event) -> None:
        self.set_class(event.size.width < 110, "-portrait")

    def activated(self) -> None:
        if self.session is None and not self._asked:
            self._asked = True
            build = self.config.build_dir or load_build(self.config.cwd)
            if build is None:
                builds = find_build_dirs(self.ws, self.config.cwd)
                build = builds[0] if len(builds) == 1 else None
            if build is not None:
                self.open_build(build)
            else:
                self.query_one("#kdetails", Static).update(
                    "Pick a build directory (t). Kconfig is read from a build, so build "
                    "in the Build tab first.")
                self.action_pick_build()
        elif self.focused is None or self not in self.focused.ancestors:
            self.tree.focus()

    # -- loading ---------------------------------------------------------

    def open_build(self, build: Path) -> None:
        self.query_one("#kdetails", Static).update(Text(f"Loading Kconfig of {build}…",
                                                        style="bright_black"))
        self._load(Path(build))

    @work(thread=True, exclusive=True, group="kconfig-load")
    def _load(self, build: Path) -> None:
        try:
            session = Session(loader.find(build))
            session.load()
        except loader.KconfigError as exc:
            self.app.call_from_thread(self._load_failed, str(exc))
            return
        self.app.call_from_thread(self._loaded, build, session)

    def _load_failed(self, error: str) -> None:
        self.notify(error, title="Kconfig", severity="error", timeout=12)
        self.query_one("#kdetails", Static).update(Text(error, style="red"))

    def _loaded(self, build: Path, session: Session) -> None:
        self.session = session
        self.config.build_dir = build
        save_build(self.config.cwd, build)
        board = session.build.board
        self.query_one("#kleft").border_title = f"Kconfig · {board}" if board else "Kconfig"
        self.current = None
        self.history = []
        self.rebuild_tree()
        self.focus_here(self.tree)
        if self.tree.cursor_node is None and self.tree.root.children:
            self.call_after_refresh(self.tree.move_cursor, self.tree.root.children[0])

    # -- the tree --------------------------------------------------------

    def _children(self, node) -> list:
        out = []
        child = node.list
        while child is not None:
            if self._shown(child):
                out.append(child)
            child = child.next
        return out

    def _shown(self, node) -> bool:
        if node.prompt is None:
            return self.show_all and isinstance(node.item, (self.lib.Symbol, self.lib.Choice))
        return self.show_all or describe.visible(self.session, node)

    def _label(self, node) -> Text:
        lib, s = self.lib, self.session
        item = node.item
        prompt = node.prompt[0] if node.prompt else ""
        dim = not describe.visible(s, node)
        t = Text()
        if item is lib.MENU:
            t.append(prompt, style="bright_black" if dim else "bold")
        elif item is lib.COMMENT:
            t.append(f"— {prompt}", style="bright_black italic")
        elif isinstance(item, lib.Choice):
            t.append(prompt or "choice", style="bright_black" if dim else "")
            t.append(": ", style="bright_black")
            t.append_text(describe.value_text(s, item))
        else:
            sym = item
            if describe.is_bool(s, sym):
                v = sym.str_value
                t.append("● " if v == "y" else "◐ " if v == "m" else "○ ",
                         style="green" if v in ("y", "m") and not dim else "bright_black")
                t.append(prompt or sym.name, style="bright_black" if dim else "")
            else:
                t.append(prompt or sym.name, style="bright_black" if dim else "")
                t.append(": ", style="bright_black")
                v = describe.value_text(s, sym)
                if dim:
                    v.stylize("bright_black")
                t.append_text(v)
            t.append(f"  {sym.name}", style="bright_black")
            if sym.name in self._user_set:
                t.append("  ✎", style="yellow")
            built = s.model.built.get(sym.name)
            if (built if built is not None else "n") != (sym.str_value or "n") and \
                    sym.config_string:
                t.append("  *", style="magenta")
        return t

    def _add_children(self, parent: TreeNode, node) -> None:
        if id(node) in self.populated:
            return
        self.populated.add(id(node))
        for child in self._children(node):
            expandable = child.list is not None and bool(self._children(child))
            item = parent.add(self._label(child), data=child, allow_expand=expandable)
            self.items[id(child)] = item

    def rebuild_tree(self) -> None:
        if self.session is None:
            return
        tree = self.tree
        expanded = [n.data for n in self.items.values()
                    if n.data is not None and n.is_expanded]
        cursor = tree.cursor_node.data if tree.cursor_node else None
        self._user_set = {name for name, lst in self.session.assignments().items()
                          if any(a.file in self.session.build.user_files for a in lst)}
        tree.clear()
        self.items, self.populated = {}, set()
        self._add_children(tree.root, self.session.kconf.top_node)
        for node in expanded:
            self._expand_to(node, open_it=True)
        if cursor is not None:
            self.reveal_node(cursor)
        self.refresh_side()
        self.update_status()

    def _expand_to(self, node, open_it: bool = False) -> TreeNode | None:
        """The tree node for `node`, unfolding (and filling) its parents."""
        chain = []
        p = node
        top = self.session.kconf.top_node
        while p is not None and p is not top:
            chain.append(p)
            p = p.parent
        item = None
        for n in reversed(chain):
            item = self.items.get(id(n))
            if item is None:
                return None
            if n is not node or open_it:
                self._add_children(item, n)
                if item.allow_expand:
                    item.expand()
        return item

    def reveal_node(self, node) -> bool:
        item = self._expand_to(node)
        if item is None:
            return False
        self.call_after_refresh(self.tree.move_cursor, item)
        return True

    def reveal_symbol(self, sym) -> bool:
        for node in sym.nodes:
            if self._shown(node) and self.reveal_node(node):
                return True
        return False

    @on(Tree.NodeExpanded, "#ktree")
    def _expanded(self, event: Tree.NodeExpanded) -> None:
        if event.node.data is not None:
            self._add_children(event.node, event.node.data)

    @on(Tree.NodeHighlighted, "#ktree")
    def _highlighted(self, event: Tree.NodeHighlighted) -> None:
        node = event.node.data
        if node is None or self.session is None:
            return
        item = node.item
        self.current = item if isinstance(item, (self.lib.Symbol, self.lib.Choice)) else node
        self.refresh_side()

    # -- right side ------------------------------------------------------

    def refresh_side(self) -> None:
        details = self.query_one("#kdetails", Static)
        related = self.query_one("#krelated", RelatedList)
        related.clear_options()
        self.related_rows = []
        if self.session is None or self.current is None:
            details.update("")
            return
        details.update(describe.details(self.session, self.current, self.ws.topdir))
        rows = describe.related(self.session, self.current)
        self.related_rows = [name for _, name, _ in rows]
        related.add_options([Option(t) for _, _, t in rows])
        panel = self.query_one("#krelated-panel")
        panel.border_title = f"Related ({len(rows)})" if rows else "Related"
        file = self.session.write_file(getattr(self.current, "name", None))
        panel.border_subtitle = (f"changes go to {describe.rel(file, self.ws.topdir)}"
                                 if file else "")

    def show_symbol(self, sym, remember: bool = True) -> None:
        if remember and self.current is not None and self.current is not sym:
            self.history.append(self.current)
        self.current = sym
        self.reveal_symbol(sym)  # hidden options stay on the right only
        self.refresh_side()

    def update_status(self, message: str | None = None) -> None:
        if message is not None:
            self.message = message
        s = self.session
        parts = []
        if s is not None:
            try:
                parts.append(f"build dir: {s.build.build_dir.relative_to(self.ws.topdir)}")
            except ValueError:
                parts.append(f"build dir: {s.build.build_dir}")
            pending = s.pending()
            if pending:
                parts.append(f"{len(pending)} option(s) not built yet: rebuild to apply")
            problems = s.problems()
            if problems:
                parts.append(f"{len(problems)} problem(s), p: list")
            if self.show_all:
                parts.append("showing hidden options (A)")
        if self.message:
            parts.append(self.message)
        self.query_one("#kstatus", Static).update("  ·  ".join(parts))

    # -- changing --------------------------------------------------------

    def _target(self):
        """The option the keys act on: the highlighted search result or tree item."""
        if self.query_one("#kresults").has_class("visible") and \
                self.focused is self.query_one("#kresults"):
            lst = self.query_one("#kresults", ResultList)
            if lst.highlighted is not None and lst.highlighted < len(self.results):
                return self.results[lst.highlighted]
            return None
        return self.current

    def report(self, result: Result | None) -> None:
        if result is None:
            return
        if result.ok:
            self.notify(result.message, timeout=4)
            self.rebuild_tree()
            self._refresh_results()
            self.update_status(result.message)
        else:
            self.notify(result.message, severity="error", timeout=10)

    def _set(self, sym, value: str) -> None:
        s = self.session
        result = s.set_values([(sym.name, value)])
        if result.ok or not result.needs:
            self.report(result)
            return
        needs = result.needs

        def confirmed(yes: bool) -> None:
            if yes:
                self.report(s.set_values([(n, "y") for n in needs] + [(sym.name, value)]))
        listing = ", ".join(f"CONFIG_{n}" for n in needs)
        self.push_screen(ConfirmScreen(f"Turn on CONFIG_{sym.name}?",
                                       f"It needs {listing}. Turn "
                                       f"{'that' if len(needs) == 1 else 'those'} on too? "
                                       "(y / n)"), confirmed)

    def action_switch(self) -> None:
        item = self._target()
        if self.session is None or item is None:
            return
        lib = self.lib
        if isinstance(item, lib.Symbol) and describe.is_bool(self.session, item):
            if item.choice is not None:
                self._set(item, "y")
            else:
                self._set(item, "n" if item.str_value in ("y", "m") else "y")
        else:
            self.action_change()

    def action_change(self) -> None:
        item = self._target()
        if self.session is None or item is None:
            return
        lib, s = self.lib, self.session
        if isinstance(item, lib.MenuNode):
            tree_item = self.items.get(id(item))
            if tree_item is not None and tree_item.allow_expand:
                tree_item.toggle()
            return
        if isinstance(item, lib.Choice):
            def choices(token: str) -> list:
                out = []
                for sym in item.syms:
                    label = describe.prompt_of(sym) or sym.name
                    if token.lower() in (label + sym.name).lower():
                        t = Text(label, style="bold")
                        t.append(f"  {sym.name}", style="bright_black")
                        if item.selection is sym:
                            t.append("  current", style="green italic")
                        out.append((sym.name, t))
                return out
            current = item.selection.name if item.selection else ""
            self.push_screen(PickScreen(describe.prompt_of(item) or "Choice",
                                        "Pick one.", choices,
                                        lambda v: s.set_values([(v, "y")]), current,
                                        free=False, placeholder="type to search"),
                             self.report)
            return
        sym = item
        if describe.is_bool(s, sym):
            self.action_switch()
            return
        if not describe.can_set(sym):
            self.notify(f"CONFIG_{sym.name} can't be set in a conf file (no prompt)",
                        severity="warning")
            return
        hint = {lib.INT: "a number", lib.HEX: "a hex number, e.g. 0x1000",
                lib.STRING: "text"}.get(sym.type, "a value")
        rng = describe.range_text(s, sym) if sym.type in (lib.INT, lib.HEX) else ""
        desc = describe.prompt_of(sym)
        if rng:
            desc += f"  ·  range{rng}"
        self.push_screen(PickScreen(f"CONFIG_{sym.name}", desc, lambda t: [],
                                    lambda v: s.set_values([(sym.name, v)]), sym.str_value,
                                    placeholder=hint), self.report)

    def action_remove(self) -> None:
        item = self._target()
        if self.session is None or not isinstance(item, self.lib.Symbol):
            return
        result = self.session.remove(item.name)
        if result.ok:
            result.message += "  (u: undo)"
        self.report(result)

    def action_undo(self) -> None:
        if self.session is not None:
            self.report(self.session.undo())

    def action_reload(self) -> None:
        if self.config.build_dir is not None:
            self.open_build(self.config.build_dir)

    # -- related ---------------------------------------------------------

    def _related_symbol(self):
        lst = self.query_one("#krelated", RelatedList)
        if lst.highlighted is None or lst.highlighted >= len(self.related_rows):
            return None
        return self.session.symbol(self.related_rows[lst.highlighted])

    def action_jump(self) -> None:
        sym = self._related_symbol()
        if sym is not None:
            self.show_symbol(sym)
            self.action_focus_main()

    def action_switch_related(self) -> None:
        sym = self._related_symbol()
        if sym is not None and describe.is_bool(self.session, sym):
            self._set(sym, "n" if sym.str_value in ("y", "m") else "y")

    def action_back(self) -> None:
        if self.history:
            self.show_symbol(self.history.pop(), remember=False)

    def action_focus_main(self) -> None:
        if self.query_one("#kresults").has_class("visible"):
            self.query_one("#kresults").focus()
        else:
            self.tree.focus()

    # -- search ----------------------------------------------------------

    def action_search(self) -> None:
        box = self.query_one("#ksearch", SearchInput)
        box.add_class("visible")
        box.focus()

    @on(Input.Changed, "#ksearch")
    def _search_changed(self, event: Input.Changed) -> None:
        self._refresh_results(event.value)

    @on(Input.Submitted, "#ksearch")
    def _search_submitted(self) -> None:
        if self.results:
            lst = self.query_one("#kresults", ResultList)
            lst.focus()
            if lst.highlighted is None:
                lst.highlighted = 0

    def _refresh_results(self, query: str | None = None) -> None:
        box = self.query_one("#ksearch", SearchInput)
        query = box.value if query is None else query
        lst = self.query_one("#kresults", ResultList)
        tree = self.tree
        if not query.strip() or self.session is None:
            lst.remove_class("visible")
            tree.display = True
            return
        keep = lst.highlighted
        self.results = search(self.session, query)
        lst.clear_options()
        s = self.session
        opts = []
        for sym in self.results:
            t = Text()
            v = describe.value_text(s, sym)
            if describe.is_bool(s, sym):
                t.append("● " if sym.str_value == "y" else "○ ",
                         style="green" if sym.str_value == "y" else "bright_black")
            t.append(sym.name, style="bold")
            if not describe.is_bool(s, sym):
                t.append(" = ")
                t.append_text(v)
            if describe.prompt_of(sym):
                t.append(f"  {describe.prompt_of(sym)}", style="bright_black")
            if not any(describe.visible(s, n) for n in sym.nodes):
                t.append("  hidden", style="bright_black italic")
            opts.append(Option(t))
        lst.add_options(opts)
        lst.add_class("visible")
        tree.display = False
        if opts:
            lst.highlighted = min(keep or 0, len(opts) - 1)

    @on(OptionList.OptionHighlighted, "#kresults")
    def _result_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        if 0 <= event.option_index < len(self.results):
            self.current = self.results[event.option_index]
            self.refresh_side()

    def action_clear_search(self) -> None:
        box = self.query_one("#ksearch", SearchInput)
        box.value = ""
        box.remove_class("visible")
        self.query_one("#kresults").remove_class("visible")
        self.tree.display = True
        if self.current is not None and isinstance(self.current, self.lib.Symbol):
            self.reveal_symbol(self.current)
        self.tree.focus()

    # -- the rest --------------------------------------------------------

    def action_toggle_all(self) -> None:
        if self.session is None:
            return
        self.show_all = not self.show_all
        self.rebuild_tree()
        self.notify("showing hidden options too" if self.show_all
                    else "showing only options you can change")

    def action_problems(self) -> None:
        if self.session is None:
            return

        def go(problem: Problem | None) -> None:
            if problem is not None and problem.symbol:
                sym = self.session.symbol(problem.symbol)
                if sym is not None:
                    self.show_symbol(sym)
        self.push_screen(KProblemsScreen(self.session.problems()), go)

    def action_pick_build(self) -> None:
        def done(build: Path | None) -> None:
            if build is not None:
                self.open_build(build)
        self.push_screen(BuildScreen(self.ws, self.config.cwd, self.config.build_dir), done)

    def action_help(self) -> None:
        self.push_screen(KHelpScreen())

    def action_open_editor(self) -> None:
        item = self.current
        if self.session is None or item is None:
            return
        lib = self.lib
        target = None
        if isinstance(item, lib.Symbol):
            lst = self.session.assigned(item.name)
            if lst:
                target = (lst[-1].file, lst[-1].line)
        if target is None:
            nodes = item.nodes if hasattr(item, "nodes") else [item]
            if nodes:
                n = nodes[0]
                p = Path(n.filename)
                if not p.is_absolute():
                    p = Path(self.session.build.env.get("srctree", "")) / p
                target = (p, n.linenr)
        if target is None:
            return
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


def search(session: Session, query: str, limit: int = 300) -> list:
    """Options matching every word of `query`: name matches first, then prompts,
    then help texts."""
    words = [w.lower().removeprefix("config_") for w in query.split()]
    if not words:
        return []
    scored = []
    for sym in session.kconf.unique_defined_syms:
        if not sym.nodes:
            continue
        name = sym.name.lower()
        prompt = describe.prompt_of(sym).lower()
        help_text = " ".join(getattr(n, "help", None) or "" for n in sym.nodes).lower()
        rank = None
        if all(w in name for w in words):
            rank = 0 if name.startswith(words[0]) else 1
        elif all(w in name or w in prompt for w in words):
            rank = 2
        elif all(w in name or w in prompt or w in help_text for w in words):
            rank = 3
        if rank is not None:
            scored.append((rank, len(name), name, sym))
    scored.sort(key=lambda x: x[:3])
    return [x[3] for x in scored[:limit]]


class LazyKconfigApp(App):
    """The Kconfig tab on its own (used by the tests)."""

    CSS = COMMON_CSS + KCONFIG_CSS + """
    ConfirmScreen, PickScreen { align: center middle; }
    .dialog { width: 100; max-width: 95%; height: auto; max-height: 90%; padding: 1 2; }
    .hint { color: $text-muted; }
    #dts-confirm-dialog { width: 70; }
    #pick-dialog { width: 100; height: auto; max-height: 80%; }
    #pick-input { margin-top: 1; }
    #pick-list { height: auto; max-height: 16; border: none; background: $surface; }
    """
    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, config: AppConfig):
        super().__init__()
        self.view = KconfigView(config, id="kconfig-view")

    def compose(self) -> ComposeResult:
        yield self.view
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self.view.activated)
