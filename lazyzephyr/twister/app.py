"""The twister tab: browse, queue and watch twister test runs."""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, OptionList, RichLog, SelectionList, Static, Tree
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from . import cache, folders
from .discovery import Discovery, Scenario, Workspace, default_roots, discover
from .jobs import Job, JobManager, NodeRef, RunOptions, RunTarget, make_target
from .results import ResultStore, worst
from .watcher import WatchError, watch_changes
from ..view import COMMON_CSS, ForwardActions, View

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
STATUS_ICON = {
    "passed": ("✔", "green"),
    "failed": ("✘", "red"),
    "error": ("!", "bold red"),
    "blocked": ("⊘", "red"),
    "skipped": ("○", "bright_black"),
    "filtered": ("○", "bright_black"),
    "not run": ("◇", "cyan"),  # built but not executed (build_only / no runner)
    "queued": ("…", "yellow"),
    "cancelled": ("⊘", "bright_black"),
    None: ("·", "bright_black"),
}
JOB_ICON = {
    "queued": ("…", "yellow"),
    "passed": ("✔", "green"),
    "failed": ("✘", "red"),
    "error": ("!", "bold red"),
    "cancelled": ("⊘", "bright_black"),
}
FAILED_STATUSES = ("failed", "error", "blocked")


def count_text(statuses: list[str | None]) -> Text:
    """`passed/failed/not run` counts in green/red/blue, e.g. `2/0/1`.

    "Not run" is everything that neither passed nor failed: skipped, filtered,
    build-only, queued, running or never run.
    """
    passed = statuses.count("passed")
    failed = sum(statuses.count(s) for s in FAILED_STATUSES)
    other = len(statuses) - passed - failed
    text = Text()
    text.append(str(passed), style="green")
    text.append("/", style="bright_black")
    text.append(str(failed), style="red")
    text.append("/", style="bright_black")
    text.append(str(other), style="blue")
    return text




@dataclass
class AppConfig:
    cwd: Path
    roots: list[Path]
    workspace: Workspace
    twister_cmd: list[str]
    state_dir: Path
    options: RunOptions = field(default_factory=RunOptions)
    watch_paths: list[Path] = field(default_factory=list)
    auto_run: bool = False
    config_file: Path | None = None
    # Where the roots came from: "cli", "config", "saved" or "default".
    roots_source: str = "default"
    # Open the folder browser on start (first run in this directory).
    pick_folders: bool = False


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------


class TestTree(ForwardActions, Tree[NodeRef]):
    BINDINGS = [
        Binding("enter", "run_cursor", "Queue"),
        Binding("space", "toggle_mark", "Mark"),
        Binding("r", "run_marked", "Queue marked"),
        Binding("c", "clear_marks", "Unmark", show=False),
        Binding("e", "edit", "Edit"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("l,right", "expand", show=False),
        Binding("h,left", "collapse", show=False),
        Binding("o", "toggle_node", "Fold", show=False),
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


class JobList(ForwardActions, OptionList):
    BINDINGS = [
        Binding("x", "cancel_job", "Cancel"),
        Binding("K,shift+up", "move_job(-1)", "Move up"),
        Binding("J,shift+down", "move_job(1)", "Move down"),
        Binding("r", "rerun_job", "Rerun"),
        Binding("L", "clear_jobs", "Clear done"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]


class Panel(Vertical):
    """Bordered container that `z` can maximize as a whole."""

    ALLOW_MAXIMIZE = True


class ScrollPanel(VerticalScroll):
    ALLOW_MAXIMIZE = True


class FilterInput(ForwardActions, Input):
    BINDINGS = [Binding("escape", "clear_filter", "Clear filter")]


class PlatformScreen(ModalScreen[list[str] | None]):
    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("enter,ctrl+s", "apply", "Apply", priority=True),
    ]

    def __init__(self, known: list[str], selected: list[str]):
        super().__init__()
        self.known = list(dict.fromkeys([*selected, *known]))
        self.selected = selected

    def compose(self) -> ComposeResult:
        with Vertical(id="platform-dialog", classes="panel") as dialog:
            dialog.border_title = "Platforms"
            dialog.border_subtitle = "space: toggle · enter: apply · esc: cancel"
            yield Static("None selected = twister defaults (plus -G integration platforms).")
            yield SelectionList[str](
                *[(p, p, p in self.selected) for p in self.known], id="platform-list"
            )
            yield Input(placeholder="other platforms, comma separated", id="platform-extra")

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def action_apply(self) -> None:
        chosen = list(self.query_one(SelectionList).selected)
        extra = self.query_one("#platform-extra", Input).value
        chosen += [p.strip() for p in extra.split(",") if p.strip()]
        self.dismiss(list(dict.fromkeys(chosen)))


class FolderTree(Tree[Path]):
    BINDINGS = [
        Binding("space", "screen.toggle", "Select"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("l,right", "expand", show=False),
        Binding("h,left", "collapse", show=False),
    ]

    action_expand = TestTree.action_expand
    action_collapse = TestTree.action_collapse


class FolderScreen(ModalScreen[list[Path] | None]):
    """Browse the directories under `base` and choose the test roots."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("enter,ctrl+s", "apply", "Apply", priority=True),
    ]

    def __init__(self, base: Path, selected: list[Path], first_run: bool = False):
        super().__init__()
        self.base = base.resolve()
        self.selected: set[Path] = set(folders.normalize(selected))
        self.first_run = first_run
        self.counts: dict[Path, int] = {}
        self.counting: set[Path] = set()
        self.nodes: dict[Path, TreeNode[Path]] = {}
        self.loaded: set[Path] = set()

    def compose(self) -> ComposeResult:
        with Vertical(id="folder-dialog", classes="panel") as dialog:
            dialog.border_title = "Test folders"
            dialog.border_subtitle = "space: select · enter: apply · esc: cancel"
            intro = (
                "First time here: choose the folders to look for tests in. "
                if self.first_run else ""
            )
            yield Static(intro + "The choice is remembered for this directory.",
                         id="folder-intro")
            tree = FolderTree(f"{self.base.name or self.base}/", data=self.base, id="folder-tree")
            tree.guide_depth = 3
            tree.auto_expand = False
            yield tree
            yield Static(id="folder-summary")

    @property
    def tree(self) -> FolderTree:
        return self.query_one(FolderTree)

    def on_mount(self) -> None:
        tree = self.tree
        self.nodes[self.base] = tree.root
        self._load(tree.root)
        tree.root.expand()
        # Reveal the current selection.
        for path in self.selected:
            if self.base in path.parents:
                for ancestor in reversed(path.parents):
                    if ancestor == self.base or self.base in ancestor.parents:
                        node = self.nodes.get(ancestor)
                        if node is not None:
                            self._load(node)
                            node.expand()
        self._count(self.base)
        self.refresh_labels()
        first = next(iter(sorted(self.selected)), None)
        if first in self.nodes:
            tree.move_cursor(self.nodes[first])
        tree.focus()

    def _load(self, node: TreeNode[Path]) -> None:
        path = node.data
        if path is None or path in self.loaded:
            return
        self.loaded.add(path)
        for child in folders.list_dirs(path):
            has_children = bool(folders.list_dirs(child))
            self.nodes[child] = node.add(self.label_for(child), data=child,
                                         allow_expand=has_children)
            if child not in self.counts:
                self._count(child)

    @on(Tree.NodeExpanded, "#folder-tree")
    def _expanded(self, event: Tree.NodeExpanded) -> None:
        self._load(event.node)
        self.refresh_labels()

    def _count(self, top: Path) -> None:
        if top not in self.counting:
            self.counting.add(top)
            self._count_worker(top)

    @work(thread=True, group="count")
    def _count_worker(self, top: Path) -> None:
        counts = folders.count_tests(top)
        try:
            self.app.call_from_thread(self._counted, counts)
        except RuntimeError:  # app already gone
            pass

    def _counted(self, counts: dict[Path, int]) -> None:
        for path, count in counts.items():
            self.counts.setdefault(path, count)
        if self.is_mounted:
            self.refresh_labels()

    def _state(self, path: Path) -> str:
        if path in self.selected:
            return "selected"
        if any(s in path.parents for s in self.selected):
            return "inside"
        if any(path in s.parents for s in self.selected):
            return "partial"
        return ""

    def label_for(self, path: Path) -> Text:
        text = Text()
        state = self._state(path)
        if state == "selected":
            text.append("[✔] ", style="bold green")
        elif state == "inside":
            text.append("[·] ", style="green")
        elif state == "partial":
            text.append("[-] ", style="yellow")
        else:
            text.append("[ ] ", style="bright_black")
        name = "./" if path == self.base else f"{path.name}/"
        count = self.counts.get(path)
        text.append(name, style="bold" if count else ("" if count is None else "bright_black"))
        if count is None:
            text.append("  …", style="bright_black")
        elif count:
            text.append(f"  {count} test file{'s' if count != 1 else ''}", style="cyan")
        return text

    def refresh_labels(self) -> None:
        for path, node in self.nodes.items():
            node.set_label(self.label_for(path))
        summary = Text()
        chosen = sorted(self.selected)
        if not chosen:
            summary.append("Nothing selected.", style="yellow")
        else:
            summary.append("Selected: ", style="bold")
            summary.append(", ".join(self._rel(p) for p in chosen))
        self.query_one("#folder-summary", Static).update(summary)

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.base)) or "."
        except ValueError:
            return str(path)

    def action_toggle(self) -> None:
        node = self.tree.cursor_node
        if node is None or node.data is None:
            return
        path = node.data
        if path in self.selected:
            self.selected.discard(path)
        else:
            # Selecting a folder replaces any selection inside it or around it.
            self.selected = {
                s for s in self.selected if path not in s.parents and s not in path.parents
            }
            self.selected.add(path)
        self.refresh_labels()

    def action_dismiss_none(self) -> None:
        self.dismiss(None)

    def action_apply(self) -> None:
        if not self.selected:
            self.notify("select at least one folder (space)", severity="warning", timeout=3)
            return
        self.dismiss(folders.normalize(list(self.selected)))


HELP_TEXT = """\
[b]Panels[/]   1 tests · 3 jobs · 4 output · 5 details · tab cycles
           2 full status (the bar at the bottom is the short version)

[b]Tests[/]
  enter      queue node under cursor (folder → whole folder, scenario, single ztest case)
  space      mark / unmark node (multi-select)
  r          queue marked nodes as ONE job (one twister invocation)
  c          clear marks
  e          open testcase.yaml / test source in $EDITOR
  O          choose test folders (file browser)
  h/l j/k    navigate, collapse / expand
  /          filter        f   only failed

[b]Global[/]
  a          queue all test roots        F   queue failed scenarios
  p          choose platforms            g   toggle integration mode (-G)
  w          toggle auto-run on change   ctrl+r  rediscover tests
  .          repeat the last run         ctrl+x  clear queue + stop running job
  C          clear the cache (twister build output, optionally results)
  z          maximize / restore panel    v   layout: auto → landscape → portrait
  q          quit

Layout adapts to the terminal: two columns when wide, stacked when narrow or
tall, and compact (details as a popup via 5) when small.

[b]Jobs[/]   one job runs at a time; the rest wait in the queue
  x cancel (queued or running) · J/K move queued job later/earlier
  r requeue · L clear finished

Single ztest cases are filtered with the native_sim `-test=` option,
so they only narrow the run on native (posix) platforms.
"""


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "app.pop_screen", "Close")]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog", classes="panel") as dialog:
            dialog.border_title = "Keybindings"
            yield Static(HELP_TEXT)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y,enter", "answer(True)", "Yes"),
        Binding("n,escape,q", "answer(False)", "No"),
    ]

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog", classes="panel") as dialog:
            dialog.border_title = "Quit?"
            dialog.border_subtitle = "y: yes · n: no"
            yield Static(self.question)

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class CacheScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("b,enter", "choose('build')", "Build output"),
        Binding("a", "choose('all')", "Everything"),
        Binding("n,escape,q", "choose(None)", "Cancel"),
    ]

    def __init__(self, state_dir: Path, builds: int, results: int):
        super().__init__()
        self.state_dir, self.builds, self.results = state_dir, builds, results

    def compose(self) -> ComposeResult:
        with Vertical(id="cache-dialog", classes="panel") as dialog:
            dialog.border_title = "Clear cache"
            dialog.border_subtitle = "b: build output · a: everything · esc: cancel"
            text = Text()
            text.append(f"{self.state_dir}\n\n", style="bright_black")
            text.append("b", style="bold")
            text.append(f"  delete twister build output ({cache.human(self.builds)}); "
                        "test results are kept\n")
            text.append("a", style="bold")
            text.append(f"  also delete the results history ({cache.human(self.results)})")
            yield Static(text)

    def action_choose(self, choice: str | None) -> None:
        self.dismiss(choice)


class InfoScreen(ModalScreen[None]):
    """Shows a panel that compact layout hides (status / details) as a popup."""

    BINDINGS = [Binding("escape,q,2,5", "app.pop_screen", "Close")]

    def __init__(self, title: str, content: Text):
        super().__init__()
        self.title_text = title
        self.content = content

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="info-dialog", classes="panel") as dialog:
            dialog.border_title = self.title_text
            dialog.border_subtitle = "esc: close"
            yield Static(self.content)


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


TWISTER_CSS = """
    TwisterView #main { layout: horizontal; height: 1fr; }
    TwisterView #left { width: 45%; min-width: 40; }
    TwisterView #right { width: 1fr; }
    TwisterView #tests-panel { height: 1fr; }
    TwisterView #tests { border: none; background: $surface; padding: 0; }
    TwisterView #filter { display: none; border: none; height: 1; padding: 0 1; }
    TwisterView #filter.visible { display: block; }
    TwisterView #statusbar {
        height: 1; padding: 0 1;
        background: $panel; color: $text-muted;
    }
    TwisterView #jobs { height: 9; }
    TwisterView #log { height: 1fr; }
    TwisterView #details-panel { height: 40%; padding: 0 1; }
    #platform-dialog, #help-dialog, #info-dialog, #confirm-dialog, #cache-dialog,
    #folder-dialog {
        width: 90; max-width: 95%; height: auto; max-height: 90%;
        padding: 1 2; background: $surface;
    }
    PlatformScreen, HelpScreen, InfoScreen, ConfirmScreen, CacheScreen, FolderScreen {
        align: center middle;
    }
    #confirm-dialog { width: 60; }
    #folder-dialog { height: 90%; }
    #folder-tree { height: 1fr; margin: 1 0; background: $surface; }

    /* Portrait: stack the two columns. */
    TwisterView.-portrait #main { layout: vertical; }
    TwisterView.-portrait #left, TwisterView.-portrait #right { width: 100%; min-width: 0; }
    TwisterView.-portrait #left { height: 45%; }
    TwisterView.-portrait #right { height: 1fr; }
    TwisterView.-portrait #jobs { height: 7; }
    TwisterView.-portrait #details-panel { height: 35%; }

    /* Compact: too small for everything — status/details become popups. */
    TwisterView.-compact #details-panel { display: none; }
    TwisterView.-compact #jobs { height: 5; }
    TwisterView.-compact.-portrait #left { height: 55%; }
    TwisterView.-compact #left { min-width: 30; }
    #platform-list { height: auto; max-height: 20; margin: 1 0; }
    """


class TwisterView(View):

    BINDINGS = [
        Binding("question_mark", "help", "Help"),
        Binding("a", "run_all", "Queue all"),
        Binding("F", "rerun_failed", "Queue failed"),
        Binding("full_stop", "repeat_last", "Repeat"),
        Binding("p", "platforms", "Platform"),
        Binding("O", "choose_folders", "Folders"),
        Binding("g", "toggle_integration", "-G", show=False),
        Binding("w", "toggle_autorun", "Auto-run"),
        Binding("slash", "filter", "Filter"),
        Binding("f", "toggle_failed_only", "Failed only", show=False),
        Binding("ctrl+x", "cancel_all", "Clear queue", show=False),
        Binding("ctrl+r", "rediscover", "Rediscover", show=False),
        Binding("z", "toggle_maximize", "Zoom"),
        Binding("v", "cycle_layout", "Layout", show=False),
        Binding("C", "clear_cache", "Clear cache", show=False),
        Binding("1", "focus_panel('tests')", show=False),
        Binding("2", "focus_panel('status')", show=False),
        Binding("3", "focus_panel('jobs')", show=False),
        Binding("4", "focus_panel('log')", show=False),
        Binding("5", "focus_panel('details-panel')", show=False),
    ]

    def __init__(self, config: AppConfig, **kwargs):
        super().__init__(**kwargs)
        self.config = config
        self.options = config.options
        self.auto_run = config.auto_run
        self.discovery = Discovery(config.roots, [], {})
        self.store = ResultStore(config.state_dir / "results.json")
        self.jobs = JobManager(
            twister_cmd=config.twister_cmd,
            state_dir=config.state_dir,
            cwd=config.cwd,
            on_change=self._on_job_change,
            on_line=self._on_job_line,
            on_finished=self._on_job_finished,
        )
        self.marks: dict[str, NodeRef] = {}
        self.node_index: dict[str, TreeNode[NodeRef]] = {}
        self.folder_scenarios: dict[str, list[str]] = defaultdict(list)
        self.live: dict[str, str] = {}  # scenario -> queued/running/live result
        self.filter_text = ""
        self.failed_only = False
        self.viewed_job: int | None = None
        self.last_target: RunTarget | None = None
        self._tree_built = False
        self.expanded: set[str] | None = None  # open folder/scenario keys
        self.seen_folders: set[str] = set()
        self.populated: set[str] = set()
        self.folder_children: dict[Path, list[Path]] = {}
        self.folder_items: dict[Path, list[Scenario]] = {}
        self._open_now: set[str] = set()
        self.loading_tests = False
        self._filter_timer = None
        self.watching = False  # file watcher is up
        self._dirty = False
        self._spin = 0
        self._followed_job: int | None = None
        self.layout_mode = "auto"  # auto | landscape | portrait
        self.orientation = "landscape"
        self.compact = False

    # -- layout ------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            with Vertical(id="left"):
                with Panel(id="tests-panel", classes="panel") as tests_panel:
                    tests_panel.border_title = "[1] Tests"
                    tree = TestTree("tests", id="tests")
                    tree.show_root = False
                    tree.auto_expand = False
                    tree.guide_depth = 3
                    yield tree
                    yield FilterInput(placeholder="filter…", id="filter")
            with Vertical(id="right"):
                jobs = JobList(id="jobs", classes="panel")
                jobs.border_title = "[3] Jobs"
                yield jobs
                log = RichLog(id="log", classes="panel", markup=False, highlight=False, wrap=False)
                log.border_title = "[4] Output"
                yield log
                with ScrollPanel(id="details-panel", classes="panel") as details:
                    details.border_title = "[5] Details"
                    yield Static(id="details")
        yield Static(id="statusbar")

    @property
    def tree(self) -> TestTree:
        return self.query_one("#tests", TestTree)

    async def on_mount(self) -> None:
        self.main_screen = self
        self.apply_layout()
        self.tree.focus()
        self.update_status()
        self.set_interval(0.15, self._tick)
        if self.config.pick_folders:
            # Asked once the tab is shown (see activated()).
            self._ask_folders = True
            return
        await self.rediscover(loading=True)
        self.watch_files()

    def activated(self) -> None:
        if getattr(self, "_ask_folders", False):
            self._ask_folders = False
            self.push_screen(
                FolderScreen(self.config.cwd, self.config.roots, first_run=True),
                self._first_folders_chosen,
            )
        elif self.focused is None or self not in self.focused.ancestors:
            self.tree.focus()

    async def _first_folders_chosen(self, roots: list[Path] | None) -> None:
        if roots is None:
            # Cancelled: use the defaults for now and ask again next time.
            self.config.roots = self.config.roots or default_roots(self.config.cwd)
        else:
            self._set_roots(roots)
        await self.rediscover(loading=True)
        self.watch_files()

    def _set_roots(self, roots: list[Path]) -> None:
        self.config.roots = roots
        self.expanded = None  # new tree: start from the default open folders
        try:
            folders.save(self.config.state_dir, self.config.cwd, roots)
        except OSError as exc:
            self.notify(f"could not save folder selection: {exc}", severity="error")
        if self.config.roots_source in ("cli", "config"):
            where = "-T" if self.config.roots_source == "cli" else "roots in the config file"
            self.notify(f"saved, but {where} take precedence on the next start",
                        severity="warning", timeout=6)
        self.config.roots_source = "saved"

    def action_choose_folders(self) -> None:
        async def chosen(roots: list[Path] | None) -> None:
            if roots is None or roots == self.config.roots:
                return
            self._set_roots(roots)
            await self.rediscover(loading=True)
            self.watch_files()  # exclusive worker: restarts on the new roots
            self.notify(f"{len(self.discovery.scenarios)} scenarios in "
                        f"{len(roots)} folder{'s' if len(roots) != 1 else ''}", timeout=3)

        self.push_screen(FolderScreen(self.config.cwd, self.config.roots), chosen)

    # -- adaptive layout ------------------------------------------------------

    # Terminal cells are ~2x taller than wide, so a "square" window is about
    # twice as many columns as rows.
    PORTRAIT_MAX_WIDTH = 110

    @staticmethod
    def pick_layout(width: int, height: int, mode: str = "auto") -> tuple[str, bool]:
        """Return (orientation, compact) for a terminal size."""
        if mode == "auto":
            portrait = width < TwisterView.PORTRAIT_MAX_WIDTH or width < 2 * height
            orientation = "portrait" if portrait else "landscape"
        else:
            orientation = mode
        if orientation == "portrait":
            compact = height < 40 or width < 50
        else:
            compact = height < 24 or width < 90
        return orientation, compact

    def apply_layout(self, size=None) -> None:
        screen = getattr(self, "main_screen", None)
        if screen is None:
            return
        width, height = size or self.size
        self.orientation, self.compact = self.pick_layout(width, height, self.layout_mode)
        screen.set_class(self.orientation == "portrait", "-portrait")
        screen.set_class(self.compact, "-compact")
        # Hidden panels can't keep focus.
        if self.compact and self.focused is not None and self.focused.id == "details-panel":
            self.tree.focus()
        self.refresh_jobs()

    def on_resize(self, event) -> None:
        # Runs before App updates self.size, so use the event's size.
        self.apply_layout(event.size)

    def action_cycle_layout(self) -> None:
        modes = ["auto", "landscape", "portrait"]
        self.layout_mode = modes[(modes.index(self.layout_mode) + 1) % len(modes)]
        self.apply_layout()
        self.notify(f"layout: {self.layout_mode} ({self.orientation}"
                    f"{', compact' if self.compact else ''})", timeout=2)

    def action_toggle_maximize(self) -> None:
        screen = self.screen
        if screen.maximized is not None:
            screen.minimize()
        elif self.focused is not None:
            screen.maximize(self.focused)

    # -- discovery & tree ----------------------------------------------------

    async def rediscover(self, loading: bool = False) -> None:
        """Rediscover tests. `loading` shows a spinner (first load / new roots);
        file-change reloads stay quiet."""
        if loading:
            self.loading_tests = True
            self.tree.loading = True
            self.update_status()
        try:
            self.discovery = await asyncio.to_thread(discover, self.config.roots)
        finally:
            self.loading_tests = False
            self.tree.loading = False
        self.rebuild_tree()
        self.update_status()
        self.update_details()

    def _matches(self, scenario: Scenario) -> bool:
        if self.failed_only and self.status_of(scenario.name) not in ("failed", "error", "blocked"):
            return False
        if not self.filter_text:
            return True
        needle = self.filter_text.lower()
        haystack = [scenario.name, str(scenario.directory), *scenario.tags]
        haystack += [c.id for c in scenario.cases]
        return any(needle in h.lower() for h in haystack)

    # Trees with more scenarios than this open only their roots at first, and
    # filter results are only expanded up to this many matches.
    OPEN_ALL_MAX = 300

    def rebuild_tree(self) -> None:
        """Rebuild the tree model, then create nodes for what is visible.

        Children of a folder or scenario are only created when it is expanded,
        so huge test roots (e.g. zephyr/tests) stay fast.
        """
        tree = self.tree
        cursor_key = None
        if tree.cursor_node is not None and tree.cursor_node.data is not None:
            cursor_key = tree.cursor_node.data.key

        # Model: folders -> subfolders / scenarios, from the matching scenarios.
        self.folder_scenarios = defaultdict(list)
        self.folder_children: dict[Path, list[Path]] = defaultdict(list)
        self.folder_items: dict[Path, list[Scenario]] = defaultdict(list)
        known_folders: set[Path] = set()
        placed: set[str] = set()
        shown = 0
        # Walk up the folders once per test directory, not once per scenario.
        by_dir: dict[Path, list[Scenario]] = defaultdict(list)
        for scenario in self.discovery.scenarios:
            by_dir[scenario.directory].append(scenario)
        for root in self.discovery.roots:
            known_folders.add(root)
            root_str = str(root)
            prefix = root_str.rstrip(os.sep) + os.sep
            for directory, scenarios in by_dir.items():
                dir_str = str(directory)
                if dir_str != root_str and not dir_str.startswith(prefix):
                    continue
                names = []
                for scenario in scenarios:
                    if scenario.name in placed:
                        continue
                    placed.add(scenario.name)
                    if self._matches(scenario):
                        names.append(scenario.name)
                        self.folder_items[directory].append(scenario)
                if not names:
                    continue
                shown += len(names)
                path = directory
                while True:
                    self.folder_scenarios[f"d:{path}"].extend(names)
                    if path not in known_folders:
                        known_folders.add(path)
                        self.folder_children[path.parent].append(path)
                    if path == root or path.parent == path:
                        break
                    path = path.parent
        for children in self.folder_children.values():
            children.sort(key=lambda p: p.name)

        folder_keys = {f"d:{p}" for p in known_folders}
        if self.expanded is None:  # first build (or new roots)
            self.expanded = {f"d:{root}" for root in self.discovery.roots}
            self.seen_folders = set()
        if shown <= self.OPEN_ALL_MAX:
            # Small tree: folders open by default, including ones added since.
            self.expanded |= folder_keys - self.seen_folders
        self.seen_folders |= folder_keys
        self._open_now = set(self.expanded)
        if (self.filter_text or self.failed_only) and shown <= self.OPEN_ALL_MAX:
            self._open_now |= {f"d:{p}" for p in known_folders}

        tree.clear()
        self.node_index = {}
        self.populated: set[str] = set()
        for root in self.discovery.roots:
            self._add_folder(tree.root, root)

        # Drop marks for nodes that no longer exist.
        self.marks = {k: v for k, v in self.marks.items() if self._ref_exists(v)}
        self._update_mark_subtitle()
        self._tree_built = True
        if cursor_key and cursor_key in self.node_index:
            tree.move_cursor(self.node_index[cursor_key])

    def _ref_exists(self, ref: NodeRef) -> bool:
        if ref.kind == "folder":
            return ref.key in self.folder_scenarios
        scenario = self.discovery.by_name().get(ref.scenario or "")
        if scenario is None:
            return False
        return ref.kind == "scenario" or any(c == ref.case for c in scenario.cases)

    def _add_node(self, parent: TreeNode, ref: NodeRef, expandable: bool) -> TreeNode:
        if expandable:
            node = parent.add(self.label_for(ref), data=ref, allow_expand=True)
        else:
            node = parent.add_leaf(self.label_for(ref), data=ref)
        self.node_index[ref.key] = node
        return node

    def _add_folder(self, parent: TreeNode, path: Path) -> None:
        ref = NodeRef("folder", path)
        node = self._add_node(parent, ref, expandable=True)
        if ref.key in self._open_now:
            self._populate(node)
            node.expand()

    def _populate(self, node: TreeNode[NodeRef]) -> None:
        """Create the children of a folder or scenario node (once)."""
        ref = node.data
        if ref is None or ref.key in self.populated:
            return
        self.populated.add(ref.key)
        if ref.kind == "folder":
            for scenario in self.folder_items.get(ref.path, []):
                child = NodeRef("scenario", scenario.directory, scenario.name)
                child_node = self._add_node(node, child, expandable=bool(scenario.cases))
                if child.key in self._open_now:
                    self._populate(child_node)
                    child_node.expand()
            for sub in self.folder_children.get(ref.path, []):
                self._add_folder(node, sub)
        elif ref.kind == "scenario":
            scenario = self.discovery.by_name().get(ref.scenario or "")
            for case in scenario.cases if scenario else []:
                self._add_node(node, NodeRef("case", ref.path, ref.scenario, case),
                               expandable=False)

    @on(Tree.NodeExpanded, "#tests")
    def _test_node_expanded(self, event: Tree.NodeExpanded) -> None:
        self._populate(event.node)
        if event.node.data is not None:
            self.expanded.add(event.node.data.key)

    @on(Tree.NodeCollapsed, "#tests")
    def _test_node_collapsed(self, event: Tree.NodeCollapsed) -> None:
        if event.node.data is not None:
            self.expanded.discard(event.node.data.key)

    # -- status helpers -----------------------------------------------------

    def status_of(self, scenario: str) -> str | None:
        return self.live.get(scenario) or self.store.scenario_status(scenario)

    def _icon(self, status: str | None) -> tuple[str, str]:
        if status == "running":
            return SPINNER[self._spin % len(SPINNER)], "yellow"
        return STATUS_ICON.get(status, STATUS_ICON[None])

    def label_for(self, ref: NodeRef) -> Text:
        text = Text()
        if ref.key in self.marks:
            text.append("◆ ", style="bold magenta")
        if ref.kind == "folder":
            names = self.folder_scenarios.get(ref.key, [])
            statuses = [self.status_of(n) for n in names]
            status = (
                "running" if "running" in statuses
                else "queued" if "queued" in statuses
                else worst(statuses)
            )
            icon, style = self._icon(status)
            text.append(f"{icon} ", style=style)
            text.append(f"{ref.path.name}/", style="bold")
            text.append("  ")
            text.append_text(count_text(statuses))
        elif ref.kind == "scenario":
            assert ref.scenario is not None
            icon, style = self._icon(self.status_of(ref.scenario))
            text.append(f"{icon} ", style=style)
            text.append(ref.scenario)
            scenario = self.discovery.by_name().get(ref.scenario)
            if scenario and scenario.build_only:
                text.append("  build-only", style="bright_black italic")
        else:
            assert ref.case is not None and ref.scenario is not None
            live = self.live.get(ref.scenario)
            status = live if live in ("running", "queued") else self.store.case_status(
                ref.scenario, ref.case.id
            )
            icon, style = self._icon(status)
            text.append(f"{icon} ", style=style)
            text.append(ref.case.suite, style="bright_black")
            text.append(".")
            text.append(ref.case.name)
        return text

    def refresh_labels(self, only_running: bool = False) -> None:
        for key, node in self.node_index.items():
            ref = node.data
            if ref is None:
                continue
            if only_running:
                names = (
                    self.folder_scenarios.get(key, []) if ref.kind == "folder" else [ref.scenario]
                )
                if not any(self.live.get(n) == "running" for n in names):
                    continue
            node.set_label(self.label_for(ref))

    def recompute_live(self) -> None:
        live: dict[str, str] = {}
        for job in self.jobs.jobs:
            if not job.active:
                continue
            for name in job.target.covered:
                state = job.live_status.get(name) or job.state
                if live.get(name) != "running":
                    live[name] = state
        self.live = live

    def _tick(self) -> None:
        if not self.is_running:  # shutting down: the widgets may be gone
            return
        self._spin += 1
        if self._dirty:
            self._dirty = False
            self.recompute_live()
            self.refresh_labels()
            self.refresh_jobs()
            self.update_status()
        elif self.jobs.running():
            self.refresh_labels(only_running=True)
            self.refresh_jobs()

    # -- panels ---------------------------------------------------------------

    def update_status(self) -> None:
        self.query_one("#statusbar", Static).update(self._status_line())

    def _status_line(self) -> Text:
        """The one-line status bar at the bottom; `2` shows the full status."""
        names = [s.name for s in self.discovery.scenarios]
        statuses = [self.store.scenario_status(n) for n in names]
        topdir = self.config.workspace.topdir
        sep = (" · ", "bright_black")
        text = Text(no_wrap=True, overflow="ellipsis")
        text.append(topdir.name if topdir else "no west workspace", style="bold")
        text.append(*sep)
        text.append(", ".join(self._rel(r) for r in self.config.roots))
        text.append(*sep)
        text.append(", ".join(self.options.platforms) or "default platforms")
        if self.options.integration:
            text.append(*sep)
            text.append("-G")
        if self.auto_run:
            text.append(*sep)
            text.append("auto-run", style="yellow")
        text.append(*sep)
        if self.loading_tests:
            text.append("loading tests…", style="yellow")
        else:
            text.append(f"{len(names)} scenarios ")
            text.append_text(count_text(statuses))
        if self.discovery.errors:
            text.append(*sep)
            text.append(f"{len(self.discovery.errors)} yaml errors", style="bold red")
        return text

    def _status_text(self) -> Text:
        cfg = self.config
        on = Text("on", style="green")
        off = Text("off", style="bright_black")
        names = [s.name for s in self.discovery.scenarios]
        statuses = [self.store.scenario_status(n) for n in names]
        text = Text()

        def row(label: str, value) -> None:
            text.append(f"{label:<12}", style="bold")
            text.append(value if isinstance(value, Text) else Text(str(value)))
            text.append("\n")

        row("workspace", cfg.workspace.topdir or "(not in a west workspace)")
        row("roots", f"{', '.join(self._rel(r) for r in cfg.roots)}  "
            f"({cfg.roots_source}; O to change)")
        row("config", cfg.config_file or "none (.lazyzephyr.toml)")
        row("platforms", ", ".join(self.options.platforms) or "twister default")
        row("integration", on if self.options.integration else off)
        row("auto-run", on if self.auto_run else off)
        current = self.jobs.current
        row("queue", f"{'running #' + str(current.id) if current else 'idle'}"
            f", {len(self.jobs.queued())} queued")
        cases = sum(len(s.cases) for s in self.discovery.scenarios)
        summary = Text(f"{len(names)} scenarios, {cases} cases  ")
        summary.append_text(count_text(statuses))
        row("tests", summary)
        if self.discovery.errors:
            row("yaml errors", Text(str(len(self.discovery.errors)), style="bold red"))
        text.rstrip()
        return text

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.config.cwd)) or "."
        except ValueError:
            return str(path)

    def job_order(self) -> list[Job]:
        """Display order: running job, queue in execution order, then history."""
        done = [j for j in reversed(self.jobs.jobs) if not j.active]
        return [*self.jobs.running(), *self.jobs.queued(), *done]

    def refresh_jobs(self) -> None:
        job_list = self.query_one(JobList)
        options = []
        order = self.job_order()
        for job in order:
            if job.state == "running":
                icon, style = SPINNER[self._spin % len(SPINNER)], "yellow"
            else:
                icon, style = JOB_ICON.get(job.state, ("?", ""))
            prompt = Text()
            prompt.append(f"{icon} ", style=style)
            prompt.append(f"#{job.id} ", style="bright_black")
            prompt.append(job.target.title)
            position = self.jobs.position(job)
            if position is not None:
                prompt.append(f"  queued {position}", style="yellow")
            if job.total:
                prompt.append(f"  {job.done}/{job.total}", style="cyan")
            if job.started_at:
                prompt.append(f"  {job.elapsed:.0f}s", style="bright_black")
            options.append(Option(prompt, id=str(job.id)))
        highlighted = self.viewed_job
        job_list.clear_options()
        job_list.add_options(options)
        if highlighted is not None:
            for index, job in enumerate(order):
                if job.id == highlighted:
                    job_list.highlighted = index
                    break
        job_list.border_subtitle = self.short_status()

    def short_status(self) -> str:
        """Queue summary shown in the Jobs panel's subtitle."""
        parts = ["running" if self.jobs.current else "idle"]
        if self.jobs.queued():
            parts.append(f"{len(self.jobs.queued())} queued")
        return " · ".join(parts)

    def show_job_log(self, job: Job | None) -> None:
        log = self.query_one("#log", RichLog)
        log.clear()
        if job is None:
            log.border_title = "[4] Output"
            return
        self.viewed_job = job.id
        log.border_title = f"[4] Output — #{job.id} {job.target.title}"
        for line in job.lines[-5000:]:
            log.write(Text.from_ansi(line))

    def update_details(self) -> None:
        details = self.query_one("#details", Static)
        node = self.tree.cursor_node
        ref = node.data if node is not None else None
        if ref is None:
            text = Text("Select a test.", style="bright_black")
            if self.discovery.errors:
                text = Text()
                text.append("YAML parse errors\n", style="bold red")
                for path, error in self.discovery.errors.items():
                    text.append(f"{self._rel(path)}: ", style="bold")
                    text.append(f"{error}\n")
            details.update(text)
            return
        details.update(self._details_for(ref))

    def _details_for(self, ref: NodeRef) -> Text:
        text = Text()

        def row(label: str, value) -> None:
            text.append(f"{label:<12}", style="bold")
            text.append(value if isinstance(value, Text) else Text(str(value)))
            text.append("\n")

        if ref.kind == "folder":
            names = self.folder_scenarios.get(ref.key, [])
            row("folder", self._rel(ref.path))
            row("scenarios", len(names))
            bad = [n for n in names if self.status_of(n) in ("failed", "error", "blocked")]
            if bad:
                text.append("\nfailing\n", style="bold red")
                for name in bad:
                    text.append(f"  ✘ {name}\n", style="red")
            text.append("\nenter: run folder (twister -T)", style="bright_black")
            return text

        scenario = self.discovery.by_name().get(ref.scenario or "")
        if scenario is None:
            return Text("Scenario no longer exists.", style="bright_black")
        if ref.kind == "case":
            assert ref.case is not None
            row("case", ref.case.id)
            row("function", ref.case.func)
            row("source", f"{self._rel(ref.case.file)}:{ref.case.line}")
            row("scenario", scenario.name)
            for platform, result in self.store.for_scenario(scenario.name).items():
                status = result.cases.get(ref.case.id)
                icon, style = self._icon(status)
                row(platform, Text(f"{icon} {status or 'no result'}", style=style))
            failure = self.failure_for(ref)
            if failure is not None:
                row("failed at", self._failure_text(*failure))
            hint = "\nenter: queue only this case (native targets, via -test=)"
            if failure is not None and failure[1] is not None:
                hint += " · e: open at failing assert"
            text.append(hint, style="bright_black")
            return text

        row("scenario", scenario.name)
        row("yaml", self._rel(scenario.yaml_file))
        if scenario.tags:
            row("tags", ", ".join(scenario.tags))
        if scenario.platform_allow:
            row("allow", ", ".join(scenario.platform_allow))
        if scenario.integration_platforms:
            row("integration", ", ".join(scenario.integration_platforms))
        if scenario.harness:
            row("harness", scenario.harness)
        if scenario.build_only:
            row("build only", "yes")
        row("cases", f"{len(scenario.cases)} in {len(scenario.suites)} suites")
        results = self.store.for_scenario(scenario.name)
        if results:
            text.append("\nlast results\n", style="bold")
        for platform, result in results.items():
            icon, style = self._icon(result.status)
            line = Text(f"  {icon} {platform:<28} {result.status}", style=style)
            line.append(f"  build {result.build_time:.1f}s  run {result.execution_time:.2f}s",
                        style="bright_black")
            text.append(line)
            text.append("\n")
            if result.reason and result.status != "passed":
                text.append(f"    reason: {result.reason}\n", style="red")
            failed_cases = [c for c, s in result.cases.items() if s in ("failed", "error")]
            for case_id in failed_cases:
                text.append(f"    ✘ {case_id}", style="red")
                failure = result.failures.get(case_id)
                if failure is not None:
                    text.append("  ")
                    text.append(self._failure_text(
                        failure, self.resolve_source(failure["file"], scenario)))
                text.append("\n")
            if result.log and result.status in ("failed", "error"):
                tail = result.log.strip().splitlines()[-40:]
                text.append("    ── log tail ──\n", style="bright_black")
                for log_line in tail:
                    text.append("    ")
                    text.append(Text.from_ansi(log_line))
                    text.append("\n")
        return text

    # -- job callbacks ----------------------------------------------------------

    def _on_job_change(self, job: Job) -> None:
        self._dirty = True
        # Follow the queue: show each job's output as soon as it starts.
        if job is self.jobs.current and job.id != self._followed_job:
            self._followed_job = job.id
            self.show_job_log(job)

    def _on_job_line(self, job: Job, line: str) -> None:
        if job.id == self.viewed_job:
            self.query_one("#log", RichLog).write(Text.from_ansi(line))

    def _on_job_finished(self, job: Job) -> None:
        if job.results:
            self.store.merge(job.results, job.target.only_cases)
            try:
                self.store.save()
            except OSError as exc:
                self.notify(f"could not save results: {exc}", severity="error")
        self.recompute_live()
        if self.failed_only:
            self.rebuild_tree()
        self._dirty = True
        self.update_details()
        if job.state == "cancelled":
            return
        statuses = [r.status for r in job.results]
        passed = statuses.count("passed")
        failed = sum(statuses.count(s) for s in ("failed", "error", "blocked"))
        message = f"#{job.id} {job.target.title}: {passed} passed, {failed} failed"
        if job.state == "error":
            self.notify(f"#{job.id} could not start twister — see output", severity="error")
        elif failed or job.state == "failed":
            self.notify(message, severity="error", timeout=8)
            self.bell()
        else:
            self.notify(message, timeout=4)

    # -- running ------------------------------------------------------------------

    def submit(self, refs: list[NodeRef]) -> None:
        try:
            target = make_target(refs, self.discovery)
        except ValueError as exc:
            self.notify(str(exc), severity="warning")
            return
        self._submit_target(target)

    def _submit_target(self, target: RunTarget) -> None:
        options = RunOptions(
            platforms=list(self.options.platforms),
            integration=self.options.integration,
            inline_logs=self.options.inline_logs,
            verbose=self.options.verbose,
            extra_args=list(self.options.extra_args),
            jobs=self.options.jobs,
        )
        job = self.jobs.submit(target, options)
        if job is None:
            self.notify("already queued", severity="warning", timeout=2)
            return
        self.last_target = target
        position = self.jobs.position(job)
        if position is not None:
            self.notify(f"queued #{job.id} ({position} waiting)", timeout=2)
        self._dirty = True

    def _cursor_ref(self) -> NodeRef | None:
        node = self.tree.cursor_node
        return node.data if node is not None else None

    def action_run_cursor(self) -> None:
        ref = self._cursor_ref()
        if ref is not None:
            self.submit([ref])

    def action_toggle_mark(self) -> None:
        ref = self._cursor_ref()
        if ref is None:
            return
        if ref.key in self.marks:
            del self.marks[ref.key]
        else:
            self.marks[ref.key] = ref
        self.node_index[ref.key].set_label(self.label_for(ref))
        self.tree.action_cursor_down()
        self._update_mark_subtitle()

    def _update_mark_subtitle(self) -> None:
        panel = self.query_one("#tests-panel")
        panel.border_subtitle = f"{len(self.marks)} marked" if self.marks else ""

    def action_clear_marks(self) -> None:
        self.marks.clear()
        self.refresh_labels()
        self._update_mark_subtitle()

    def _marked_or_cursor(self) -> list[NodeRef]:
        if self.marks:
            return list(self.marks.values())
        ref = self._cursor_ref()
        return [ref] if ref else []

    def action_run_marked(self) -> None:
        refs = self._marked_or_cursor()
        if refs:
            self.submit(refs)
            self.action_clear_marks()

    def action_run_all(self) -> None:
        self.submit([NodeRef("folder", root) for root in self.discovery.roots])

    def action_rerun_failed(self) -> None:
        by_name = self.discovery.by_name()
        failed = [n for n in self.store.failed_scenarios() if n in by_name]
        if not failed:
            self.notify("no failed scenarios", timeout=2)
            return
        self.submit([NodeRef("scenario", by_name[n].directory, n) for n in failed])

    # -- jobs panel -----------------------------------------------------------------

    def _selected_job(self) -> Job | None:
        job_list = self.query_one(JobList)
        if job_list.highlighted is None:
            return None
        option = job_list.get_option_at_index(job_list.highlighted)
        return next((j for j in self.jobs.jobs if str(j.id) == option.id), None)

    @on(OptionList.OptionHighlighted, "#jobs")
    def _job_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        job = next((j for j in self.jobs.jobs if str(j.id) == event.option.id), None)
        if job is not None and job.id != self.viewed_job:
            self.show_job_log(job)

    def action_cancel_job(self) -> None:
        job = self._selected_job()
        if job is not None and job.active:
            self.jobs.cancel(job)

    def action_move_job(self, delta: int) -> None:
        job = self._selected_job()
        if job is not None and self.jobs.move(job, delta):
            self.viewed_job = job.id
            self.refresh_jobs()

    def action_rerun_job(self) -> None:
        job = self._selected_job()
        if job is not None:
            self._submit_target(job.target)

    def action_clear_jobs(self) -> None:
        self.jobs.clear_finished()
        if self.viewed_job not in {j.id for j in self.jobs.jobs}:
            self.viewed_job = None
            self.show_job_log(None)
        self.refresh_jobs()

    def action_cancel_all(self) -> None:
        self.jobs.cancel_all()

    # -- settings --------------------------------------------------------------------

    def action_platforms(self) -> None:
        def apply(result: list[str] | None) -> None:
            if result is not None:
                self.options.platforms = result
                self.update_status()

        self.push_screen(PlatformScreen(self.discovery.platforms(), self.options.platforms), apply)

    def action_toggle_integration(self) -> None:
        self.options.integration = not self.options.integration
        self.update_status()

    def action_toggle_autorun(self) -> None:
        self.auto_run = not self.auto_run
        self.notify(f"auto-run on file change {'on' if self.auto_run else 'off'}", timeout=2)
        self.update_status()

    def action_filter(self) -> None:
        box = self.query_one("#filter", FilterInput)
        box.add_class("visible")
        box.focus()

    def action_clear_filter(self) -> None:
        box = self.query_one("#filter", FilterInput)
        box.value = ""
        box.remove_class("visible")
        self.tree.focus()

    FILTER_DELAY = 0.15  # seconds; rebuild once typing pauses

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        if self._filter_timer is not None:
            self._filter_timer.stop()
        text = event.value.strip()

        def apply() -> None:
            self._filter_timer = None
            if text != self.filter_text:
                self.filter_text = text
                self.rebuild_tree()

        if not text:
            apply()  # clearing (e.g. esc) is instant
        else:
            self._filter_timer = self.set_timer(self.FILTER_DELAY, apply)

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        if not event.value.strip():
            event.input.remove_class("visible")
        self.tree.focus()

    def action_toggle_failed_only(self) -> None:
        self.failed_only = not self.failed_only
        self.rebuild_tree()
        self.query_one("#tests-panel").border_title = (
            "[1] Tests (failed only)" if self.failed_only else "[1] Tests"
        )

    async def action_rediscover(self) -> None:
        await self.rediscover(loading=True)
        self.notify(f"{len(self.discovery.scenarios)} scenarios", timeout=2)

    def action_focus_panel(self, panel_id: str) -> None:
        if panel_id == "status":
            self.push_screen(InfoScreen("Status", self._status_text()))
        elif self.compact and panel_id == "details-panel":
            node = self.tree.cursor_node
            ref = node.data if node is not None else None
            content = self._details_for(ref) if ref else Text("Select a test.")
            self.push_screen(InfoScreen("Details", content))
        else:
            self.query_one(f"#{panel_id}").focus()

    def action_request_quit(self) -> None:
        current, queued = self.jobs.current, len(self.jobs.queued())
        if current is None and not queued:
            self.exit()
            return
        parts = []
        if current is not None:
            parts.append(f"#{current.id} {current.target.title} is still running")
        if queued:
            parts.append(f"{queued} job{'s' if queued != 1 else ''} waiting in the queue")
        question = " and ".join(parts) + ".\nQuit and stop twister?"

        def answered(yes: bool | None) -> None:
            if yes:
                self.exit()

        self.push_screen(ConfirmScreen(question), answered)

    async def action_clear_cache(self) -> None:
        if self.jobs.current is not None or self.jobs.queued():
            self.notify("stop or finish the queued jobs before clearing the cache",
                        severity="warning")
            return
        state_dir = self.config.state_dir
        builds, results = await asyncio.to_thread(cache.usage, state_dir)

        async def chosen(choice: str | None) -> None:
            if choice is None:
                return
            everything = choice == "all"
            try:
                freed = await asyncio.to_thread(cache.clear, state_dir, everything)
            except OSError as exc:
                self.notify(f"could not clear cache: {exc}", severity="error")
                return
            if everything:
                self.store.clear()
                self.recompute_live()
                self.refresh_labels()
                self.update_status()
                self.update_details()
            self.notify(f"freed {cache.human(freed)}", timeout=3)

        self.push_screen(CacheScreen(state_dir, builds, results), chosen)

    def action_repeat_last(self) -> None:
        if self.last_target is None:
            self.notify("nothing to repeat yet", timeout=2)
        else:
            self._submit_target(self.last_target)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_edit(self) -> None:
        ref = self._cursor_ref()
        if ref is None or ref.kind == "folder":
            return
        failure = self.failure_for(ref)
        if failure is not None and failure[1] is not None:
            # Jump straight to the failing assertion.
            path, line = failure[1], failure[0]["line"]
        elif ref.kind == "case":
            assert ref.case is not None
            path, line = ref.case.file, ref.case.line
        else:
            scenario = self.discovery.by_name().get(ref.scenario or "")
            if scenario is None:
                return
            path, line = scenario.yaml_file, 1
        editor = shlex.split(os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi")
        try:
            with self.suspend():
                subprocess.run([*editor, f"+{line}", str(path)], check=False)
        except Exception as exc:  # SuspendNotSupported, missing editor, …
            self.notify(f"could not open editor: {exc}", severity="error")

    def resolve_source(self, file: str, scenario: Scenario) -> Path | None:
        """Map a path printed by ztest (see Zephyr's -fmacro-prefix-map) to a real file."""
        workspace = self.config.workspace
        prefixes = {
            "CMAKE_SOURCE_DIR": scenario.directory,
            "ZEPHYR_BASE": workspace.zephyr_base,
            "WEST_TOPDIR": workspace.topdir,
        }
        head, _, rest = file.partition("/")
        if head in prefixes:
            base = prefixes[head]
            candidate = base / rest if base is not None else None
        elif Path(file).is_absolute():
            candidate = Path(file)
        else:
            candidate = scenario.directory / file
        return candidate if candidate is not None and candidate.is_file() else None

    def failure_for(self, ref: NodeRef) -> tuple[dict, Path | None] | None:
        """First recorded assertion failure for a case, or for any case of a scenario."""
        scenario = self.discovery.by_name().get(ref.scenario or "")
        if scenario is None:
            return None
        for result in self.store.for_scenario(scenario.name).values():
            for case_id, failure in result.failures.items():
                if ref.kind == "scenario" or (ref.case is not None and case_id == ref.case.id):
                    return failure, self.resolve_source(failure["file"], scenario)
        return None

    def _failure_text(self, failure: dict, path: Path | None) -> Text:
        where = f"{self._rel(path) if path else failure['file']}:{failure['line']}"
        text = Text(where, style="bold red")
        if failure.get("message"):
            text.append(f"  {failure['message']}", style="red")
        return text

    @on(Tree.NodeHighlighted, "#tests")
    def _node_highlighted(self) -> None:
        self.update_details()

    # -- file watching ------------------------------------------------------------

    @work(exclusive=True, group="watch")
    async def watch_files(self) -> None:
        """Rediscover (and auto-run) on changes. Restarting the worker (it's
        exclusive) stops the previous watcher."""
        self.watching = False
        paths = [p for p in [*self.config.roots, *self.config.watch_paths] if p.exists()]
        if not paths:
            return

        def ready() -> None:
            self.watching = True

        try:
            async for changed in watch_changes(paths, on_ready=ready):
                await self.rediscover()
                if self.auto_run:
                    self._auto_run(changed)
        except (WatchError, OSError) as exc:
            self.notify(f"live reload disabled: {exc}", severity="warning", timeout=8)

    def _auto_run(self, changed: list[Path]) -> None:
        affected = [
            s for s in self.discovery.scenarios
            if any(s.directory == p or s.directory in p.parents for p in changed)
        ]
        if affected:
            names = list(dict.fromkeys(s.name for s in affected))
            by_name = self.discovery.by_name()
            self.submit([NodeRef("scenario", by_name[n].directory, n) for n in names])
        elif self.last_target is not None:
            self._submit_target(self.last_target)

    async def on_unmount(self) -> None:
        self.jobs.shutdown()


class LazyTwisterApp(App):
    """The twister tab on its own (used by the tests)."""

    CSS = COMMON_CSS + TWISTER_CSS
    BINDINGS = [Binding("q", "request_quit", "Quit")]

    def __init__(self, config: AppConfig):
        super().__init__()
        self.view = TwisterView(config, id="twister")

    def compose(self) -> ComposeResult:
        yield self.view
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self.view.activated)

    @property
    def tree(self):
        return self.view.tree

    def action_request_quit(self) -> None:
        self.view.action_request_quit()

    def __getattr__(self, name: str):
        view = self.__dict__.get("view")
        if view is not None and not name.startswith("_"):
            return getattr(view, name)
        raise AttributeError(name)
