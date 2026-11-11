"""The build tab: saved build configs, west build / flash / menuconfig and friends."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, OptionList, RichLog, Select, Static
from textual.widgets.option_list import Option

from ..dts.workspace import Board, Workspace, find_boards, find_build_dirs
from ..view import COMMON_CSS, ForwardActions, View
from . import meta
from .configs import (BuildConfig, ConfigStore, app_path, build_cmd, build_dir,
                      config_from_build, default_build_dir, dts_build_dir, is_sysbuild_image,
                      runner_cmd, shown, target_cmd)
from .runner import Job, Runner, west_command

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
STATUS = {
    "passed": ("✔", "green"),
    "failed": ("✘", "red"),
    "error": ("!", "bold red"),
    "cancelled": ("⊘", "bright_black"),
    "running": ("…", "yellow"),
    None: ("·", "bright_black"),
}

# (key, label, kind). kind: "job" streams output; "terminal" suspends the UI.
ACTIONS = [
    ("build", "Build", "job"),
    ("pristine", "Pristine build (-p always)", "job"),
    ("flash", "Flash", "job"),
    ("build_flash", "Build, then flash", "job"),
    ("erase_flash", "Flash with --erase", "job"),
    ("run", "Run (native_sim, qemu…: -t run)", "job"),
    ("menuconfig", "menuconfig", "terminal"),
    ("guiconfig", "guiconfig", "job"),
    ("hardenconfig", "hardenconfig", "job"),
    ("ram_report", "RAM report", "job"),
    ("rom_report", "ROM report", "job"),
    ("debug", "Debug (west debug)", "terminal"),
    ("attach", "Attach (west attach)", "terminal"),
    ("debugserver", "Debug server (west debugserver)", "job"),
    ("rtt", "RTT console (west rtt)", "terminal"),
    ("dts", "Open the build in the Devicetree tab", "app"),
    ("delete_dir", "Delete the build directory", "app"),
]
ACTION_LABEL = {k: label for k, label, _ in ACTIONS}


@dataclass
class BuildConfigOptions:
    """Things the config form offers, scanned once in the background."""

    boards: list[Board]
    shields: list[meta.Shield]
    snippets: list[meta.Snippet]
    apps: list[Path]


def rel(path: Path | str, base: Path) -> str:
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return str(path)


def split_list(text: str) -> list[str]:
    return [s.strip() for s in text.replace(";", ",").split(",") if s.strip()]


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------


class ConfigList(ForwardActions, OptionList):
    BINDINGS = [
        Binding("enter,space", "actions", "Actions"),
        Binding("b", "do('build')", "Build"),
        Binding("B", "do('pristine')", "Pristine"),
        Binding("f", "do('flash')", "Flash"),
        Binding("F", "do('build_flash')", "Build+flash"),
        Binding("m", "do('menuconfig')", "menuconfig", show=False),
        Binding("t", "do('dts')", "Devicetree", show=False),
        Binding("n", "new_config", "New"),
        Binding("e", "edit_config", "Edit"),
        Binding("c", "copy_config", "Copy", show=False),
        Binding("d", "delete_config", "Delete", show=False),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]


class Panel(Vertical):
    ALLOW_MAXIMIZE = True


class ScrollPanel(VerticalScroll):
    ALLOW_MAXIMIZE = True


# --------------------------------------------------------------------------
# Dialogs
# --------------------------------------------------------------------------


class PickerScreen(ModalScreen[list[str] | None]):
    """Searchable list; one pick, or several with space."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "done", "Done", priority=True),
    ]

    def __init__(self, title: str, items: list[tuple[str, Text | str, str]],
                 multi: bool = False, selected: list[str] = (), hint: str = ""):
        super().__init__()
        self.title_text, self.items, self.multi = title, items, multi
        self.selected = list(selected)
        self.hint = hint
        self.shown: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-dialog", classes="panel dialog") as dialog:
            dialog.border_title = self.title_text
            dialog.border_subtitle = ("space: toggle · enter/ctrl+s: done · esc: cancel"
                                      if self.multi else "enter: pick · esc: cancel")
            if self.hint:
                yield Static(self.hint, classes="hint")
            yield Input(placeholder="type to filter", id="picker-filter")
            yield OptionList(id="picker-list")
            if self.multi:
                yield Static("", id="picker-selected", classes="hint")

    def on_mount(self) -> None:
        self.refill()
        self.query_one("#picker-filter", Input).focus()

    @on(Input.Changed, "#picker-filter")
    def refill(self, _=None) -> None:
        words = self.query_one("#picker-filter", Input).value.lower().split()
        lst = self.query_one("#picker-list", OptionList)
        lst.clear_options()
        self.shown = []
        opts = []
        for item_id, label, search in self.items:
            if all(w in search.lower() for w in words):
                self.shown.append(item_id)
                opts.append(Option(self._label(item_id, label), id=item_id))
            if len(opts) >= 500:
                break
        lst.add_options(opts)
        if opts:
            lst.highlighted = 0
        self._show_selected()

    def _label(self, item_id: str, label) -> Text:
        text = Text()
        if self.multi:
            text.append("[✔] " if item_id in self.selected else "[ ] ",
                        style="green" if item_id in self.selected else "bright_black")
        text.append(label if isinstance(label, Text) else Text(label))
        return text

    def _show_selected(self) -> None:
        if self.multi:
            self.query_one("#picker-selected", Static).update(
                "selected: " + (", ".join(self.selected) or "none"))

    def _toggle(self) -> None:
        lst = self.query_one("#picker-list", OptionList)
        if lst.highlighted is None:
            return
        item_id = self.shown[lst.highlighted]
        if item_id in self.selected:
            self.selected.remove(item_id)
        else:
            self.selected.append(item_id)
        label = next(label for i, label, _ in self.items if i == item_id)
        lst.replace_option_prompt_at_index(lst.highlighted, self._label(item_id, label))
        self._show_selected()

    def on_key(self, event) -> None:
        lst = self.query_one("#picker-list", OptionList)
        if event.key == "space" and self.multi and self.focused is lst:
            event.stop()
            self._toggle()
        elif event.key == "down" and self.focused is self.query_one("#picker-filter"):
            event.stop()
            lst.focus()

    @on(Input.Submitted, "#picker-filter")
    def _submit_filter(self) -> None:
        if self.multi:
            self.action_done()
            return
        lst = self.query_one("#picker-list", OptionList)
        if lst.highlighted is not None and self.shown:
            self.dismiss([self.shown[lst.highlighted]])

    @on(OptionList.OptionSelected, "#picker-list")
    def _picked(self, event: OptionList.OptionSelected) -> None:
        if self.multi:
            self.action_done()
        else:
            self.dismiss([event.option.id])

    def action_done(self) -> None:
        self.dismiss(list(self.selected) if self.multi else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y,enter", "answer(True)", "Yes"),
        Binding("n,escape", "answer(False)", "No"),
    ]

    def __init__(self, title: str, message: str):
        super().__init__()
        self.title_text, self.message = title, message

    def compose(self) -> ComposeResult:
        with Vertical(id="build-confirm-dialog", classes="panel dialog") as dialog:
            dialog.border_title = self.title_text
            dialog.border_subtitle = "y: yes · n: no"
            yield Static(self.message)

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "close", "Close")]

    TEXT = """\
[b]Configs[/b]
  enter, space       all actions for the selected config
  b / B              build / pristine build (-p always)
  f / F              flash / build, then flash
  m                  menuconfig
  t                  open the build in the Devicetree tab
  n / e / c / d      new / edit / copy / delete config
  found builds       enter imports one as a config
[b]Anywhere in the tab[/b]
  x                  stop the running command
  L                  clear the output
  z                  maximize the focused panel
  ?                  this help

Only one command runs at a time. Configs are saved per workspace in
~/.cache/lazyzephyr/builds.json.
"""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="build-help-dialog", classes="panel dialog") as dialog:
            dialog.border_title = "Build help"
            yield Static(self.TEXT)

    def action_close(self) -> None:
        self.dismiss(None)


class ConfigScreen(ModalScreen[BuildConfig | None]):
    """Create or edit a build config."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    def __init__(self, view: BuildView, cfg: BuildConfig, title: str):
        super().__init__()
        self.view, self.cfg, self.title_text = view, cfg, title
        self.ws = view.ws
        self.opts = view.options

    # -- layout --

    def _row(self, label: str, widget, button: str | None = None, hint: str = ""):
        yield Static(label, classes="label")
        if button:
            with Horizontal(classes="field-row"):
                yield widget
                yield Button("…", id=button, classes="pick")
        else:
            yield widget
        if hint:
            yield Static(hint, classes="hint")

    def compose(self) -> ComposeResult:
        c = self.cfg
        with VerticalScroll(id="config-dialog", classes="panel dialog") as dialog:
            dialog.border_title = self.title_text
            dialog.border_subtitle = "ctrl+s: save · esc: cancel · …: pick from a list"
            yield from self._row("Name", Input(value=c.name, id="f-name"))
            yield from self._row("Application", Input(value=c.app, id="f-app"), "pick-app",
                                 "folder with CMakeLists.txt, relative to the workspace")
            yield from self._row("Board", Input(value=c.board, id="f-board"), "pick-board")
            yield from self._row("Revision", Select([], id="f-revision", prompt="board default"))
            yield from self._row("Shields", Input(value=", ".join(c.shields), id="f-shields"),
                                 "pick-shields")
            yield from self._row("Snippets", Input(value=", ".join(c.snippets), id="f-snippets"),
                                 "pick-snippets")
            yield from self._row("Sysbuild", Select(
                [("west default (build.sysbuild)", "default"), ("on", "on"), ("off", "off")],
                value={None: "default", True: "on", False: "off"}[c.sysbuild],
                allow_blank=False, id="f-sysbuild"))
            yield from self._row("Build directory", Input(value=c.build_dir or "", id="f-dir"),
                                 hint="")
            yield from self._row("Extra Kconfig files (EXTRA_CONF_FILE)",
                                 Input(value=", ".join(c.extra_conf), id="f-conf",
                                       placeholder="e.g. debug.conf, relative to the app"))
            yield from self._row("Extra devicetree overlays (EXTRA_DTC_OVERLAY_FILE)",
                                 Input(value=", ".join(c.extra_overlays), id="f-overlays",
                                       placeholder="e.g. boards/test.overlay"))
            yield from self._row("More CMake arguments",
                                 Input(value=" ".join(c.cmake_args), id="f-cmake",
                                       placeholder="e.g. -DCONFIG_LOG=y"))
            yield from self._row("Flash runner", Select([], id="f-runner", prompt="board default"))
            yield from self._row("Probe serial (--dev-id)", Input(value=c.dev_id or "",
                                                                  id="f-devid"), "pick-probe")
            yield from self._row("More flash arguments",
                                 Input(value=" ".join(c.flash_args), id="f-flash",
                                       placeholder="e.g. --erase"))
            yield Static("", id="f-preview", classes="preview")
            yield Static("", id="f-error")

    def on_mount(self) -> None:
        self._board_changed()
        self._preview()

    # -- helpers --

    def _val(self, wid: str) -> str:
        return self.query_one(f"#{wid}", Input).value.strip()

    def _board(self) -> Board | None:
        target = self._val("f-board")
        for b in self.opts.boards if self.opts else []:
            if b.target == target:
                return b
        # `name` alone is fine for single-SoC boards.
        if self.opts and "/" not in target:
            plain = [b for b in self.opts.boards if b.name == target and len(b.quals) == 1]
            if len(plain) == 1:
                return plain[0]
        return None

    def _board_changed(self) -> None:
        board = self._board()
        rev = self.query_one("#f-revision", Select)
        runner = self.query_one("#f-runner", Select)
        if board is None:
            rev.set_options([])
            runner.set_options([(self.cfg.runner, self.cfg.runner)] if self.cfg.runner else [])
            if self.cfg.runner:
                runner.value = self.cfg.runner
            return
        revs = meta.board_revisions(board)
        rev.set_options([(r + ("  (default)" if r == revs.default else ""), r)
                         for r in revs.names])
        rev.prompt = f"board default ({revs.default})" if revs.default else "board default"
        if self.cfg.revision in revs.names:
            rev.value = self.cfg.revision
        cfg = self._collect(check=False) or self.cfg
        runners = meta.board_runners(board, build_dir(self.ws, cfg))
        names = list(runners.names)
        if self.cfg.runner and self.cfg.runner not in names:
            names.append(self.cfg.runner)
        runner.set_options([(n + ("  (default)" if n == runners.flash_default else ""), n)
                            for n in names])
        runner.prompt = (f"board default ({runners.flash_default})" if runners.flash_default
                         else "board default")
        if self.cfg.runner in names:
            runner.value = self.cfg.runner

    def _collect(self, check: bool = True) -> BuildConfig | None:
        """The config as filled in. With check=True, report problems and return None."""
        try:
            import shlex
            cmake = shlex.split(self._val("f-cmake"))
            flash = shlex.split(self._val("f-flash"))
        except ValueError as exc:
            if check:
                self._error(f"arguments: {exc}")
            return None
        rev = self.query_one("#f-revision", Select).value
        runner = self.query_one("#f-runner", Select).value
        sysbuild = {"default": None, "on": True, "off": False}[
            self.query_one("#f-sysbuild", Select).value]
        board = self._board()
        cfg = replace(
            self.cfg,
            name=self._val("f-name"),
            app=self._val("f-app"),
            board=board.target if board else self._val("f-board"),
            revision=rev if isinstance(rev, str) else None,
            shields=split_list(self._val("f-shields")),
            snippets=split_list(self._val("f-snippets")),
            sysbuild=sysbuild,
            build_dir=self._val("f-dir") or None,
            extra_conf=split_list(self._val("f-conf")),
            extra_overlays=split_list(self._val("f-overlays")),
            cmake_args=cmake,
            runner=runner if isinstance(runner, str) else None,
            dev_id=self._val("f-devid") or None,
            flash_args=flash,
        )
        if not check:
            return cfg
        problem = self._problem(cfg, board)
        if problem:
            self._error(problem)
            return None
        return cfg

    def _problem(self, cfg: BuildConfig, board: Board | None) -> str | None:
        if not cfg.name:
            return "give the config a name"
        if any(c.name == cfg.name and c is not self.view.editing
               for c in self.view.store.configs):
            return f"there is already a config called {cfg.name}"
        if not cfg.app or not (app_path(self.ws, cfg) / "CMakeLists.txt").is_file():
            return "the application folder needs a CMakeLists.txt"
        if self.opts is None:
            return "still reading boards and shields, try again in a moment"
        if board is None:
            return f"unknown board target: {cfg.board} (use … to pick one)"
        known = {s.name for s in self.opts.shields}
        bad = [s for s in cfg.shields if s not in known]
        if bad:
            return f"unknown shield: {bad[0]}"
        known = {s.name for s in self.opts.snippets}
        bad = [s for s in cfg.snippets if s not in known]
        if bad:
            return f"unknown snippet: {bad[0]}"
        d = build_dir(self.ws, cfg)
        if d.resolve() in (self.ws.topdir.resolve(), app_path(self.ws, cfg).resolve()):
            return "the build directory can't be the workspace or the app folder"
        return None

    def _error(self, msg: str) -> None:
        self.query_one("#f-error", Static).update(f"[red]{escape(msg)}[/red]")

    def _preview(self) -> None:
        cfg = self._collect(check=False)
        if cfg is None:
            return
        self.query_one("#f-dir", Input).placeholder = \
            f"default: {rel(default_build_dir(self.ws, cfg), self.ws.topdir)}"
        text = Text()
        text.append("build: ", style="bright_black")
        text.append(shown(build_cmd(self.ws, cfg)) + "\n")
        text.append("flash: ", style="bright_black")
        text.append(shown(runner_cmd(self.ws, cfg)))
        self.query_one("#f-preview", Static).update(text)

    # -- events --

    @on(Input.Changed)
    def _changed(self, event: Input.Changed) -> None:
        if event.input.id == "f-board":
            self._board_changed()
        self._preview()
        self.query_one("#f-error", Static).update("")

    @on(Select.Changed)
    def _select_changed(self) -> None:
        self._preview()

    @on(Input.Submitted)
    def _submitted(self) -> None:
        self.focus_next()

    @on(Button.Pressed)
    def _pick(self, event: Button.Pressed) -> None:
        if self.opts is None:
            self._error("still reading boards and shields, try again in a moment")
            return
        handler = {
            "pick-app": self._pick_app, "pick-board": self._pick_board,
            "pick-shields": self._pick_shields, "pick-snippets": self._pick_snippets,
            "pick-probe": self._pick_probe,
        }[event.button.id]
        handler()

    def _set(self, wid: str, value: str) -> None:
        self.query_one(f"#{wid}", Input).value = value

    def _pick_app(self) -> None:
        top = self.ws.topdir
        items = []
        for p in self.opts.apps:
            r = rel(p, top)
            label = Text(r, style="" if not p.is_relative_to(self.ws.zephyr_base)
                         else "bright_black")
            items.append((r, label, r))
        self.app.push_screen(PickerScreen("Application", items),
                             lambda r: r and self._set("f-app", r[0]))

    def _pick_board(self) -> None:
        items = []
        for b in self.opts.boards:
            label = Text.assemble(b.target, (f"  {b.full_name or ''}", "bright_black"))
            items.append((b.target, label, f"{b.target} {b.vendor or ''} {b.full_name or ''}"))
        self.app.push_screen(PickerScreen("Board", items),
                             lambda r: r and self._set("f-board", r[0]))

    def _pick_shields(self) -> None:
        items = [(s.name, Text.assemble(s.name, (f"  {s.full_name or ''}", "bright_black")),
                  f"{s.name} {s.full_name or ''} {s.vendor or ''}") for s in self.opts.shields]
        self.app.push_screen(
            PickerScreen("Shields", items, multi=True,
                         selected=split_list(self._val("f-shields")),
                         hint="Order matters: later shields are applied after earlier ones."),
            lambda r: r is not None and self._set("f-shields", ", ".join(r)))

    def _pick_snippets(self) -> None:
        items = [(s.name, s.name, s.name) for s in self.opts.snippets]
        self.app.push_screen(
            PickerScreen("Snippets", items, multi=True,
                         selected=split_list(self._val("f-snippets"))),
            lambda r: r is not None and self._set("f-snippets", ", ".join(r)))

    def _pick_probe(self) -> None:
        probes = meta.find_probes()
        if not probes:
            self._error("no debug probes found on USB")
            return
        items = [(p.dev_id, Text.assemble(p.dev_id, (f"  {p.kind} · {p.product}",
                                                     "bright_black")),
                  f"{p.dev_id} {p.kind} {p.product}") for p in probes]
        self.app.push_screen(PickerScreen("Debug probe", items),
                             lambda r: r and self._set("f-devid", r[0]))

    def action_save(self) -> None:
        cfg = self._collect()
        if cfg:
            self.dismiss(cfg)

    def action_cancel(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# View
# --------------------------------------------------------------------------

BUILD_CSS = """
    BuildView #b-main { layout: horizontal; height: 1fr; }
    BuildView #b-left { width: 40%; min-width: 36; }
    BuildView #b-right { width: 1fr; }
    BuildView #b-configs { height: 1fr; border: none; background: $surface; }
    BuildView #b-details-panel { height: 40%; padding: 0 1; }
    BuildView #b-log { height: 1fr; }
    BuildView #b-statusbar { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    BuildView.-portrait #b-main { layout: vertical; }
    BuildView.-portrait #b-left, BuildView.-portrait #b-right { width: 100%; min-width: 0; }
    BuildView.-portrait #b-left { height: 35%; }
    PickerScreen, ConfigScreen { align: center middle; }
    #picker-dialog { height: 90%; }
    #picker-list { height: 1fr; margin: 1 0; }
    #config-dialog { width: 110; height: 90%; }
    #config-dialog .field-row { height: auto; }
    #config-dialog .field-row Input { width: 1fr; }
    #config-dialog .pick { min-width: 5; width: 5; }
    #config-dialog .preview { margin-top: 1; color: $text-muted; }
    #build-confirm-dialog { width: 70; }
    #f-error { margin-top: 1; }
"""


class BuildView(View):
    BINDINGS = [
        Binding("x", "cancel_job", "Stop"),
        Binding("L", "clear_log", "Clear output", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("z", "maximize", "Zoom", show=False),
    ]

    def __init__(self, ws: Workspace, cwd: Path, store: ConfigStore | None = None,
                 west: list[str] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.ws, self.cwd = ws, cwd
        self.store = store or ConfigStore(ws.topdir)
        self.options: BuildConfigOptions | None = None
        self.found: list[Path] = []
        self.runner = Runner(ws.topdir, self._on_line, self._on_change, west)
        self.job: Job | None = None
        self.last_job: Job | None = None
        self.editing: BuildConfig | None = None
        self._spin = 0
        self._ids: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="b-main"):
            with Panel(id="b-left", classes="panel") as left:
                left.border_title = "Build configs"
                yield ConfigList(id="b-configs")
            with Vertical(id="b-right"):
                with ScrollPanel(id="b-details-panel", classes="panel") as details:
                    details.border_title = "Details"
                    yield Static("", id="b-details")
                log = RichLog(id="b-log", classes="panel", markup=False, highlight=False,
                              wrap=False, max_lines=20000)
                log.border_title = "Output"
                yield log
        yield Static("", id="b-statusbar")

    @property
    def configs_list(self) -> ConfigList:
        return self.query_one("#b-configs", ConfigList)

    def on_mount(self) -> None:
        self.set_interval(0.15, self._tick)
        self.refresh_list()
        self.scan()

    def activated(self) -> None:
        if self.focused is None or self not in self.focused.ancestors:
            self.configs_list.focus()

    def on_resize(self, event) -> None:
        self.set_class(event.size.width < 110, "-portrait")

    @work(thread=True, exclusive=True, group="scan")
    def scan(self) -> None:
        opts = BuildConfigOptions(find_boards(self.ws), meta.find_shields(self.ws),
                                  meta.find_snippets(self.ws), meta.find_apps(self.ws))
        found = self._find_builds()
        self.call_from_thread(self._scanned, opts, found)

    def _find_builds(self) -> list[Path]:
        return [d for d in find_build_dirs(self.ws, self.cwd) if not is_sysbuild_image(d)]

    def _scanned(self, opts: BuildConfigOptions, found: list[Path]) -> None:
        self.options, self.found = opts, found
        self.refresh_list()
        self.update_status()

    # -- list --

    def _known_dirs(self) -> set[Path]:
        return {build_dir(self.ws, c).resolve() for c in self.store.configs}

    def refresh_list(self, select: str | None = None) -> None:
        lst = self.configs_list
        current = select or (self._ids[lst.highlighted]
                             if lst.highlighted is not None and lst.highlighted < len(self._ids)
                             else None)
        lst.clear_options()
        self._ids = []
        opts = []
        for i, cfg in enumerate(self.store.configs):
            self._ids.append(f"cfg:{cfg.name}")
            opts.append(Option(self._cfg_label(cfg), id=f"cfg:{cfg.name}"))
        known = self._known_dirs()
        found = [d for d in self.found if d.resolve() not in known]
        if found:
            opts.append(Option(Text("found builds (enter: import)", style="bold bright_black"),
                               disabled=True))
            self._ids.append("-")
            for d in found:
                self._ids.append(f"found:{d}")
                label = Text.assemble(("+ ", "bright_black"), (rel(d, self.ws.topdir), "italic"))
                opts.append(Option(label, id=f"found:{d}"))
        if not self.store.configs and not found:
            opts.append(Option(Text("no configs yet: press n", style="bright_black"),
                               disabled=True))
            self._ids.append("-")
        lst.add_options(opts)
        if current in self._ids:
            lst.highlighted = self._ids.index(current)
        elif self._ids and self._ids[0] != "-":
            lst.highlighted = 0
        self.show_details()

    def _cfg_label(self, cfg: BuildConfig) -> Text:
        running = self.job is not None and self.job.config == cfg.name \
            and self.job.status == "running"
        if running:
            icon, style = SPINNER[self._spin % len(SPINNER)], "yellow"
        else:
            icon, style = STATUS.get(cfg.last_status, STATUS[None])
        text = Text()
        text.append(f"{icon} ", style=style)
        text.append(cfg.name, style="bold")
        extra = [cfg.target]
        if cfg.shields:
            extra.append("+" + ",".join(cfg.shields))
        text.append("  " + " ".join(extra), style="cyan")
        return text

    def selected(self) -> tuple[str, object] | None:
        lst = self.configs_list
        if lst.highlighted is None or lst.highlighted >= len(self._ids):
            return None
        key = self._ids[lst.highlighted]
        kind, _, value = key.partition(":")
        if kind == "cfg":
            cfg = next((c for c in self.store.configs if c.name == value), None)
            return ("cfg", cfg) if cfg else None
        if kind == "found":
            return ("found", Path(value))
        return None

    def selected_config(self) -> BuildConfig | None:
        sel = self.selected()
        return sel[1] if sel and sel[0] == "cfg" else None

    @on(OptionList.OptionHighlighted, "#b-configs")
    def _highlighted(self) -> None:
        self.show_details()

    @on(OptionList.OptionSelected, "#b-configs")
    def _selected(self) -> None:
        self.action_actions()

    # -- details --

    def _board(self, cfg: BuildConfig) -> Board | None:
        if not self.options:
            return None
        return next((b for b in self.options.boards if b.target == cfg.board), None)

    def show_details(self) -> None:
        details = self.query_one("#b-details", Static)
        sel = self.selected()
        if sel is None:
            details.update("")
            return
        top = self.ws.topdir
        if sel[0] == "found":
            cfg = config_from_build(self.ws, sel[1])
            t = Text()
            t.append(f"{rel(sel[1], top)}\n", style="bold")
            if cfg:
                t.append("would import as: ", style="bright_black")
                t.append(f"{cfg.app} on {cfg.target}"
                         + (f" + {', '.join(cfg.shields)}" if cfg.shields else "") + "\n")
                t.append(shown(build_cmd(self.ws, cfg)) + "\n", style="bright_black")
            else:
                t.append("can't read its build_info.yml\n", style="red")
            t.append("\nenter imports it as a config.", style="bright_black")
            details.update(t)
            return
        cfg = sel[1]
        d = build_dir(self.ws, cfg)
        t = Text()
        t.append(cfg.name + "\n", style="bold")

        def row(label: str, value: str, style: str = "") -> None:
            t.append(f"{label:<11}", style="bright_black")
            t.append(value + "\n", style=style)

        row("app", cfg.app)
        row("board", cfg.target, "cyan")
        board = self._board(cfg)
        if board:
            revs = meta.board_revisions(board)
            if revs.names:
                row("revisions", ", ".join(revs.names) + (f" (default {revs.default})"
                                                          if revs.default else ""))
        if cfg.shields:
            row("shields", ", ".join(cfg.shields))
        if cfg.snippets:
            row("snippets", ", ".join(cfg.snippets))
        row("sysbuild", {None: "west default", True: "on", False: "off"}[cfg.sysbuild])
        built = (d / "build_info.yml").is_file() or (d / "CMakeCache.txt").is_file()
        row("build dir", rel(d, top) + ("" if cfg.build_dir else "  (default)")
            + ("" if built else "  (not built yet)"),
            "" if built else "bright_black")
        if board:
            runners = meta.board_runners(board, d if built else None)
            using = cfg.runner or runners.flash_default or "?"
            avail = ", ".join(runners.names) or "none found"
            row("runner", f"{using}" + ("" if cfg.runner else " (board default)")
                + f"   available: {avail}")
        if cfg.dev_id:
            row("probe", cfg.dev_id)
        if cfg.last_status:
            when = time.strftime("%H:%M:%S", time.localtime(cfg.last_time or 0))
            icon, style = STATUS.get(cfg.last_status, STATUS[None])
            row("last", f"{icon} {ACTION_LABEL.get(cfg.last_action or '', cfg.last_action)}"
                f" {cfg.last_status} at {when}", style)
        t.append("\n")
        t.append(shown(build_cmd(self.ws, cfg)) + "\n", style="bright_black")
        t.append(shown(runner_cmd(self.ws, cfg)) + "\n", style="bright_black")
        details.update(t)

    def update_status(self) -> None:
        parts = []
        if west_command() is None:
            parts.append("west not found on PATH: start from the dev shell")
        if self.job and self.job.status == "running":
            parts.append(f"running: {self.job.title} ({self.job.duration or 0:.0f}s) · x: stop")
        elif self.last_job:
            icon, _ = STATUS.get(self.last_job.status, STATUS[None])
            parts.append(f"{icon} {self.last_job.title}: {self.last_job.status}"
                         f" in {self.last_job.duration or 0:.0f}s")
        if self.options is None:
            parts.append("reading boards…")
        self.query_one("#b-statusbar", Static).update("  ·  ".join(parts))

    def _tick(self) -> None:
        if self.job and self.job.status == "running":
            self._spin += 1
            lst = self.configs_list
            name = f"cfg:{self.job.config}"
            if name in self._ids:
                cfg = next((c for c in self.store.configs if c.name == self.job.config), None)
                if cfg:
                    lst.replace_option_prompt_at_index(self._ids.index(name),
                                                       self._cfg_label(cfg))
            self.update_status()

    # -- jobs --

    def _on_line(self, job: Job, line: str) -> None:
        log = self.query_one("#b-log", RichLog)
        if line.startswith("$ "):
            log.write(Text(line, style="bold cyan"))
        else:
            log.write(Text.from_ansi(line))

    def _on_change(self, job: Job) -> None:
        if job.status in ("passed", "failed", "cancelled", "error"):
            cfg = next((c for c in self.store.configs if c.name == job.config), None)
            if cfg:
                cfg.last_action, cfg.last_status, cfg.last_time = \
                    job.action, job.status, time.time()
                self.store.save()
            self.last_job = job
            if job.status == "passed":
                self.notify(f"{job.title}: done", timeout=3)
            elif job.status != "cancelled":
                self.notify(f"{job.title}: {job.status}", severity="error", timeout=6)
            self.found = self._find_builds() if job.action in (
                "build", "pristine", "build_flash") else self.found
            self.refresh_list()
        self.update_status()

    def start(self, cfg: BuildConfig, action: str, steps: list[list[str]]) -> None:
        if self.runner.busy:
            self.notify("a command is already running (x stops it)", severity="warning")
            return
        title = f"{ACTION_LABEL.get(action, action)} · {cfg.name}"
        self.job = Job(title, cfg.name, action, steps)
        log = self.query_one("#b-log", RichLog)
        log.write(Text(f"── {title} ──", style="bold"))
        self.run_job(self.job)

    @work(exclusive=True, group="job")
    async def run_job(self, job: Job) -> None:
        await self.runner.run(job)

    def _terminal(self, args: list[str]) -> None:
        west = west_command()
        if west is None:
            self.notify("west not found: start from the dev shell", severity="error")
            return
        with self.suspend():
            print("$ west " + " ".join(args), flush=True)
            subprocess.run([*west, *args], cwd=self.ws.topdir)

    def _built(self, cfg: BuildConfig) -> bool:
        d = build_dir(self.ws, cfg)
        if (d / "CMakeCache.txt").is_file() or (d / "build_info.yml").is_file():
            return True
        self.notify(f"{cfg.name} isn't built yet (b builds it)", severity="warning")
        return False

    def do(self, cfg: BuildConfig, action: str) -> None:
        ws = self.ws
        if action == "build":
            self.start(cfg, action, [build_cmd(ws, cfg)])
        elif action == "pristine":
            self.start(cfg, action, [build_cmd(ws, cfg, pristine=True)])
        elif action == "build_flash":
            self.start(cfg, action, [build_cmd(ws, cfg), runner_cmd(ws, cfg)])
        elif action == "flash":
            if self._built(cfg):
                self.start(cfg, action, [runner_cmd(ws, cfg)])
        elif action == "erase_flash":
            if self._built(cfg):
                self.start(cfg, action, [runner_cmd(ws, cfg, extra=["--erase"])])
        elif action == "debugserver":
            if self._built(cfg):
                self.start(cfg, action, [runner_cmd(ws, cfg, "debugserver")])
        elif action in ("run", "guiconfig", "hardenconfig", "ram_report", "rom_report"):
            if self._built(cfg):
                self.start(cfg, action, [target_cmd(ws, cfg, action)])
        elif action == "menuconfig":
            if self._built(cfg):
                self._terminal(target_cmd(ws, cfg, "menuconfig"))
        elif action in ("debug", "attach", "rtt"):
            if self._built(cfg):
                self._terminal(runner_cmd(ws, cfg, action))
        elif action == "dts":
            self.open_in_dts(cfg)
        elif action == "delete_dir":
            self.delete_build_dir(cfg)

    def open_in_dts(self, cfg: BuildConfig) -> None:
        d = dts_build_dir(build_dir(self.ws, cfg))
        if not (d / "build_info.yml").is_file():
            self.notify("build it first: the Devicetree tab reads build_info.yml",
                        severity="warning")
            return
        opener = getattr(self.app, "open_in_dts", None)
        if opener is None:
            self.notify("no Devicetree tab here", severity="warning")
            return
        opener(d)

    def delete_build_dir(self, cfg: BuildConfig) -> None:
        d = build_dir(self.ws, cfg)
        if not d.is_dir():
            self.notify(f"{rel(d, self.ws.topdir)} doesn't exist")
            return
        if not ((d / "CMakeCache.txt").is_file() or (d / "build_info.yml").is_file()):
            self.notify(f"{rel(d, self.ws.topdir)} doesn't look like a build directory; "
                        "not deleting it", severity="error")
            return
        if self.runner.busy:
            self.notify("stop the running command first", severity="warning")
            return

        def answered(yes: bool) -> None:
            if yes:
                shutil.rmtree(d, ignore_errors=True)
                self.found = [f for f in self.found if f != d]
                self.notify(f"deleted {rel(d, self.ws.topdir)}")
                self.refresh_list()
        self.push_screen(ConfirmScreen("Delete build directory?",
                                       f"Delete {d} and everything in it?"), answered)

    # -- actions --

    def action_do(self, action: str) -> None:
        sel = self.selected()
        if sel and sel[0] == "found":
            self.action_import()
            return
        cfg = self.selected_config()
        if cfg is None:
            self.notify("pick a config first (n makes one)")
            return
        self.do(cfg, action)

    def action_actions(self) -> None:
        sel = self.selected()
        if sel is None:
            return
        if sel[0] == "found":
            self.action_import()
            return
        cfg = sel[1]
        items = [(k, label, label) for k, label, _ in ACTIONS]
        self.push_screen(PickerScreen(f"Run for {cfg.name}", items),
                         lambda r: r and self.do(cfg, r[0]))

    def action_import(self) -> None:
        sel = self.selected()
        if not sel or sel[0] != "found":
            return
        cfg = config_from_build(self.ws, sel[1])
        if cfg is None:
            self.notify("can't read that build's build_info.yml", severity="error")
            return
        cfg.name = self.store.unique_name(cfg.name)
        self.store.configs.append(cfg)
        self.store.save()
        self.refresh_list(select=f"cfg:{cfg.name}")
        self.notify(f"imported {cfg.name}")

    def _edit(self, cfg: BuildConfig, title: str, existing: BuildConfig | None) -> None:
        self.editing = existing

        def saved(new: BuildConfig | None) -> None:
            self.editing = None
            if new is None:
                return
            if existing is not None:
                idx = self.store.configs.index(existing)
                self.store.configs[idx] = new
            else:
                self.store.configs.append(new)
            self.store.save()
            self.refresh_list(select=f"cfg:{new.name}")
        self.push_screen(ConfigScreen(self, cfg, title), saved)

    def action_new_config(self) -> None:
        base = self.selected_config()
        if base:
            cfg = replace(base, name=self.store.unique_name(base.name), last_action=None,
                          last_status=None, last_time=None, build_dir=None)
        else:
            app = self.cwd if (self.cwd / "CMakeLists.txt").is_file() else None
            cfg = BuildConfig(name="", app=rel(app, self.ws.topdir) if app else "", board="")
        cfg.name = ""
        self._edit(cfg, "New build config", None)

    def action_edit_config(self) -> None:
        cfg = self.selected_config()
        if cfg:
            self._edit(replace(cfg), f"Edit {cfg.name}", cfg)

    def action_copy_config(self) -> None:
        cfg = self.selected_config()
        if cfg:
            copy = replace(cfg, name=self.store.unique_name(cfg.name), build_dir=None,
                           last_action=None, last_status=None, last_time=None)
            self._edit(copy, f"Copy of {cfg.name}", None)

    def action_delete_config(self) -> None:
        cfg = self.selected_config()
        if cfg is None:
            return

        def answered(yes: bool) -> None:
            if yes:
                self.store.configs.remove(cfg)
                self.store.save()
                self.refresh_list()
        self.push_screen(ConfirmScreen(f"Delete {cfg.name}?",
                                       "Removes the saved config. The build directory "
                                       "stays (use the action menu to delete it)."), answered)

    def action_cancel_job(self) -> None:
        if not self.runner.cancel():
            self.notify("nothing is running")

    def action_clear_log(self) -> None:
        self.query_one("#b-log", RichLog).clear()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_maximize(self) -> None:
        if self.screen.maximized:
            self.screen.minimize()
            return
        widget = self.focused
        while widget is not None and not getattr(widget, "ALLOW_MAXIMIZE", False):
            widget = widget.parent
        if widget is not None:
            self.screen.maximize(widget)


class LazyBuildApp(App):
    """The build tab on its own (used by the tests)."""

    CSS = COMMON_CSS + BUILD_CSS + """
    .dialog { width: 100; max-width: 95%; height: auto; max-height: 90%; padding: 1 2; }
    .hint { color: $text-muted; }
    .label { margin-top: 1; }
    """
    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, view: BuildView):
        super().__init__()
        self.view = view

    def compose(self) -> ComposeResult:
        yield self.view
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self.view.activated)
