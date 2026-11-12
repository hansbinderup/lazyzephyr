"""The build tab: saved build configs, west build / flash / menuconfig and friends."""

from __future__ import annotations

import os
import shlex
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
from textual.widgets import Footer, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from ..dts.workspace import Board, Workspace, find_boards, find_build_dirs
from ..view import COMMON_CSS, ForwardActions, View
from . import meta
from .configs import (BuildConfig, ConfigStore, app_path, build_cmd, build_dir,
                      config_from_build, default_build_dir, dts_build_dir, is_sysbuild_image,
                      monitor_env, runner_cmd, shown, target_cmd)
from .monitor import FULLSCREEN_KEY, LEAVE_KEY, MonitorTerminal
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
    ("monitor", "Monitor (serial console, in the tab)", "app"),
    ("close_monitor", "Close the monitor", "app"),
    ("set_monitor", "Change the monitor command", "app"),
    ("dts", "Open the build in the Devicetree tab", "app"),
    ("kconfig", "Open the build in the Kconfig tab", "app"),
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
        Binding("o", "do('monitor')", "Monitor"),
        Binding("O", "close_monitor", "Close monitor", show=False),
        Binding("t", "do('dts')", "Devicetree", show=False),
        Binding("K", "do('kconfig')", "Kconfig", show=False),
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
  o                  monitor: open it, or go back to the running one
  O                  close the monitor (ends the connection)
  t                  open the build in the Devicetree tab
  n / e / c / d      new / edit / copy / delete config
[b]Editing a config[/b]
  j/k, arrows        move between settings (changes nothing)
  enter              change the setting (a list to pick from, or a text box)
  e                  type the value instead of picking it
  ctrl+s / esc       save / leave (asks first if something changed)
  found builds       enter imports one as a config
[b]Monitor[/b]
  The command is per config (Monitor command in the config), run with sh
  in the workspace; $BUILD_DIR and $BOARD are set. Examples:
    tio /dev/ttyACM0
    west espressif monitor -d $BUILD_DIR
  While the monitor has focus every key goes to it, except:
  ctrl+o             back to the configs (the monitor keeps running)
  f11                full screen on/off (esc also leaves full screen)
  shift+pgup/pgdn    scroll back (or the mouse wheel)
[b]Anywhere in the tab[/b]
  x                  stop the running command
  L                  clear the output
  z                  maximize the focused panel
  f11                monitor full screen on/off
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


class TextEditScreen(ModalScreen[str | None]):
    """Edit one value. enter keeps it, esc throws it away."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, value: str, placeholder: str = "", hint: str = ""):
        super().__init__()
        self.title_text, self.value, self.placeholder, self.hint = title, value, placeholder, hint

    def compose(self) -> ComposeResult:
        with Vertical(id="text-dialog", classes="panel dialog") as dialog:
            dialog.border_title = self.title_text
            dialog.border_subtitle = "enter: keep · esc: cancel"
            if self.hint:
                yield Static(self.hint, classes="hint")
            yield Input(value=self.value, placeholder=self.placeholder, id="text-value")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str          # text, list, args, app, board, shields, snippets, revision,
                       # sysbuild, runner, probe
    hint: str = ""


FIELDS = [
    Field("name", "Name", "text"),
    Field("app", "Application", "app", "folder with CMakeLists.txt, relative to the workspace"),
    Field("board", "Board", "board"),
    Field("revision", "Revision", "revision"),
    Field("shields", "Shields", "shields", "order matters: later shields go on top"),
    Field("snippets", "Snippets", "snippets"),
    Field("sysbuild", "Sysbuild", "sysbuild"),
    Field("build_dir", "Build directory", "text",
          "relative to the workspace; empty for the default"),
    Field("extra_conf", "Extra Kconfig files", "list",
          "EXTRA_CONF_FILE, comma separated, relative to the app"),
    Field("extra_overlays", "Extra overlays", "list",
          "EXTRA_DTC_OVERLAY_FILE, comma separated, relative to the app"),
    Field("cmake_args", "More CMake args", "args", "e.g. -DCONFIG_LOG=y"),
    Field("runner", "Flash runner", "runner"),
    Field("dev_id", "Probe (--dev-id)", "probe"),
    Field("flash_args", "More flash args", "args", "e.g. --erase"),
    Field("monitor", "Monitor command", "text",
          "shell command; $BUILD_DIR and $BOARD are set. "
          "e.g. tio /dev/ttyACM0 or west espressif monitor -d $BUILD_DIR"),
]


class SettingsList(OptionList):
    BINDINGS = [
        Binding("enter", "screen.edit", "Change"),
        Binding("e", "screen.edit_text", "Type it"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]


class ConfigScreen(ModalScreen[BuildConfig | None]):
    """Create or edit a build config: move freely, change a setting with enter."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "save", "Save", priority=True),
    ]

    def __init__(self, view: BuildView, cfg: BuildConfig, title: str):
        super().__init__()
        self.view, self.title_text = view, title
        self.ws = view.ws
        self.opts = view.options
        self.draft = replace(cfg)
        self.original = replace(cfg)

    def compose(self) -> ComposeResult:
        with Vertical(id="config-dialog", classes="panel dialog") as dialog:
            dialog.border_title = self.title_text
            dialog.border_subtitle = ("enter: change · e: type it · ctrl+s: save · "
                                      "esc: cancel")
            yield SettingsList(id="settings")
            yield Static("", id="f-hint", classes="hint")
            yield Static("", id="f-preview", classes="preview")
            yield Static("", id="f-error")

    def on_mount(self) -> None:
        self.refresh_rows()
        lst = self.query_one(SettingsList)
        lst.highlighted = 0
        lst.focus()

    # -- display --

    def _board(self) -> Board | None:
        target = self.draft.board
        boards = self.opts.boards if self.opts else []
        for b in boards:
            if b.target == target:
                return b
        plain = [b for b in boards if b.name == target and len(b.quals) == 1]
        return plain[0] if len(plain) == 1 else None

    def _value(self, f: Field) -> Text:
        d = self.draft
        dim = "bright_black"
        v = getattr(d, f.key)
        if f.key == "board":
            if not v:
                return Text("not set", style="red")
            return Text(v, style="cyan" if self._board() or not self.opts else "red")
        if f.key == "revision":
            board = self._board()
            default = meta.board_revisions(board).default if board else None
            return Text(v) if v else Text("board default" + (f" ({default})" if default
                                                            else ""), style=dim)
        if f.key == "sysbuild":
            return Text({None: "west default", True: "on", False: "off"}[v],
                        style=dim if v is None else "")
        if f.key == "build_dir":
            return Text(v) if v else Text(
                f"default: {rel(default_build_dir(self.ws, d), self.ws.topdir)}", style=dim)
        if f.key == "runner":
            board = self._board()
            default = meta.board_runners(board, build_dir(self.ws, d)).flash_default \
                if board else None
            return Text(v) if v else Text("board default" + (f" ({default})" if default
                                                            else ""), style=dim)
        if f.kind in ("list", "shields", "snippets"):
            return Text(", ".join(v)) if v else Text("none", style=dim)
        if f.kind == "args":
            return Text(shlex.join(v)) if v else Text("none", style=dim)
        if f.key == "app" and v and not (app_path(self.ws, d) / "CMakeLists.txt").is_file():
            return Text(v, style="red")
        return Text(v) if v else Text("not set", style="red" if f.key in ("name", "app")
                                      else dim)

    def refresh_rows(self) -> None:
        lst = self.query_one(SettingsList)
        keep = lst.highlighted
        lst.clear_options()
        width = max(len(f.label) for f in FIELDS) + 2
        rows = []
        for f in FIELDS:
            t = Text()
            changed = getattr(self.draft, f.key) != getattr(self.original, f.key)
            t.append("● " if changed else "  ", style="yellow")
            t.append(f.label.ljust(width), style="bold")
            t.append_text(self._value(f))
            rows.append(Option(t, id=f.key))
        lst.add_options(rows)
        if keep is not None:
            lst.highlighted = keep
        self._preview()

    def _preview(self) -> None:
        t = Text()
        t.append("build: ", style="bright_black")
        t.append(shown(build_cmd(self.ws, self.draft)) + "\n")
        t.append("flash: ", style="bright_black")
        t.append(shown(runner_cmd(self.ws, self.draft)))
        if self.draft.monitor:
            t.append("\nmonitor: ", style="bright_black")
            t.append(self.draft.monitor)
        self.query_one("#f-preview", Static).update(t)

    def _field(self) -> Field | None:
        lst = self.query_one(SettingsList)
        return FIELDS[lst.highlighted] if lst.highlighted is not None else None

    @on(OptionList.OptionHighlighted, "#settings")
    def _highlighted(self) -> None:
        f = self._field()
        self.query_one("#f-hint", Static).update(f.hint if f else "")

    @on(OptionList.OptionSelected, "#settings")
    def _clicked(self) -> None:
        self.action_edit()

    def _error(self, msg: str) -> None:
        self.query_one("#f-error", Static).update(f"[red]{escape(msg)}[/red]" if msg else "")

    def _set(self, key: str, value) -> None:
        setattr(self.draft, key, value)
        if key == "board":
            board = self._board()
            if board:
                self.draft.board = board.target
                revs = meta.board_revisions(board)
                if self.draft.revision and self.draft.revision not in revs.names:
                    self.draft.revision = None
            else:
                self.draft.revision = None
        self._error("")
        self.refresh_rows()

    # -- editing --

    def action_edit(self) -> None:
        f = self._field()
        if f is None:
            return
        if f.kind in ("text", "list", "args"):
            self.action_edit_text()
            return
        if self.opts is None:
            self._error("still reading boards and shields, try again in a moment")
            return
        getattr(self, f"_pick_{f.kind}")(f)

    def action_edit_text(self) -> None:
        f = self._field()
        if f is None:
            return
        if f.kind in ("revision", "sysbuild", "runner"):
            self.action_edit()  # a choice, not free text
            return
        v = getattr(self.draft, f.key)
        if f.kind == "args":
            text = shlex.join(v)
        elif isinstance(v, list):
            text = ", ".join(v)
        else:
            text = v or ""

        def done(value: str | None) -> None:
            if value is None:
                return
            if f.kind == "args":
                try:
                    self._set(f.key, shlex.split(value))
                except ValueError as exc:
                    self._error(f"{f.label}: {exc}")
            elif f.kind in ("list", "shields", "snippets"):
                self._set(f.key, split_list(value))
            elif f.key == "build_dir":
                self._set(f.key, value or None)
            elif f.key == "dev_id":
                self._set(f.key, value or None)
            else:
                self._set(f.key, value)
        self.app.push_screen(TextEditScreen(f.label, text, hint=f.hint), done)

    def _pick(self, title: str, items, key: str, multi: bool = False, selected=(),
              hint: str = "", convert=lambda r: r[0]) -> None:
        def done(result: list[str] | None) -> None:
            if result is None or (not multi and not result):
                return
            self._set(key, convert(result))
        self.app.push_screen(PickerScreen(title, items, multi=multi, selected=selected,
                                          hint=hint), done)

    def _pick_app(self, f: Field) -> None:
        items = []
        for p in self.opts.apps:
            r = rel(p, self.ws.topdir)
            style = "bright_black" if p.is_relative_to(self.ws.zephyr_base) else ""
            items.append((r, Text(r, style=style), r))
        self._pick("Application", items, "app", hint="e on the setting types a path instead")

    def _pick_board(self, f: Field) -> None:
        items = [(b.target, Text.assemble(b.target, (f"  {b.full_name or ''}", "bright_black")),
                  f"{b.target} {b.vendor or ''} {b.full_name or ''}") for b in self.opts.boards]
        self._pick("Board", items, "board")

    def _pick_shields(self, f: Field) -> None:
        items = [(s.name, Text.assemble(s.name, (f"  {s.full_name or ''}", "bright_black")),
                  f"{s.name} {s.full_name or ''} {s.vendor or ''}") for s in self.opts.shields]
        self._pick("Shields", items, "shields", multi=True, selected=self.draft.shields,
                   hint=f.hint, convert=list)

    def _pick_snippets(self, f: Field) -> None:
        items = [(s.name, s.name, s.name) for s in self.opts.snippets]
        self._pick("Snippets", items, "snippets", multi=True, selected=self.draft.snippets,
                   convert=list)

    def _pick_revision(self, f: Field) -> None:
        board = self._board()
        if board is None:
            self._error("pick a board first")
            return
        revs = meta.board_revisions(board)
        if not revs.names:
            self._error(f"{board.name} has no revisions")
            return
        items = [("", Text("board default" + (f" ({revs.default})" if revs.default else ""),
                           style="bright_black"), "default")]
        items += [(r, r + ("  (default)" if r == revs.default else ""), r) for r in revs.names]
        self._pick("Revision", items, "revision", convert=lambda r: r[0] or None)

    def _pick_sysbuild(self, f: Field) -> None:
        items = [("default", "west default (build.sysbuild)", "default"),
                 ("on", "on", "on"), ("off", "off", "off")]
        self._pick("Sysbuild", items, "sysbuild",
                   convert=lambda r: {"default": None, "on": True, "off": False}[r[0]])

    def _pick_runner(self, f: Field) -> None:
        board = self._board()
        runners = meta.board_runners(board, build_dir(self.ws, self.draft)) if board else None
        names = list(runners.names) if runners else []
        default = runners.flash_default if runners else None
        items = [("", Text("board default" + (f" ({default})" if default else ""),
                           style="bright_black"), "default")]
        items += [(n, n + ("  (default)" if n == default else ""), n) for n in names]
        hint = ("from the build's runners.yaml" if runners and runners.exact
                else "from the board's board.cmake; exact once built")
        self._pick("Flash runner", items, "runner", hint=hint, convert=lambda r: r[0] or None)

    def _pick_probe(self, f: Field) -> None:
        probes = meta.find_probes()
        if not probes:
            self._error("no debug probes found on USB; e types a serial number")
            return
        items = [("", Text("none (runner picks)", style="bright_black"), "none")]
        items += [(p.dev_id, Text.assemble(p.dev_id, (f"  {p.kind} · {p.product}",
                                                      "bright_black")),
                   f"{p.dev_id} {p.kind} {p.product}") for p in probes]
        self._pick("Debug probe", items, "dev_id", convert=lambda r: r[0] or None)

    # -- save --

    def _problem(self, cfg: BuildConfig) -> str | None:
        if not cfg.name:
            return "give the config a name"
        if any(c.name == cfg.name and c is not self.view.editing
               for c in self.view.store.configs):
            return f"there is already a config called {cfg.name}"
        if not cfg.app or not (app_path(self.ws, cfg) / "CMakeLists.txt").is_file():
            return "the application folder needs a CMakeLists.txt"
        if self.opts is None:
            return "still reading boards and shields, try again in a moment"
        if self._board() is None:
            return f"unknown board target: {cfg.board or '(none)'}"
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

    def action_save(self) -> None:
        problem = self._problem(self.draft)
        if problem:
            self._error(problem)
            return
        self.dismiss(self.draft)

    def action_cancel(self) -> None:
        if self.draft == self.original:
            self.dismiss(None)
            return

        def answered(yes: bool) -> None:
            if yes:
                self.dismiss(None)
        self.app.push_screen(ConfirmScreen("Discard changes?",
                                           "Leave without saving this config?"), answered)


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
    BuildView #b-monitor-panel { height: 2fr; display: none; }
    BuildView.-monitor #b-monitor-panel { display: block; }
    BuildView.-monitor #b-details-panel { height: 25%; }
    BuildView #b-statusbar { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    BuildView.-portrait #b-main { layout: vertical; }
    BuildView.-portrait #b-left, BuildView.-portrait #b-right { width: 100%; min-width: 0; }
    BuildView.-portrait #b-left { height: 35%; }
    PickerScreen, ConfigScreen, TextEditScreen { align: center middle; }
    #settings { height: auto; max-height: 20; margin-top: 1; }
    #text-dialog { width: 90; }
    #picker-dialog { height: 90%; }
    #picker-list { height: 1fr; margin: 1 0; }
    #config-dialog { width: 120; }
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
        Binding(FULLSCREEN_KEY, "monitor_fullscreen", "Monitor full screen", show=False),
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
        self.monitor_config: str | None = None   # config the monitor was opened for
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
                with Panel(id="b-monitor-panel", classes="panel") as mon:
                    mon.border_title = "Monitor"
                    mon.border_subtitle = (f"{LEAVE_KEY}: back · {FULLSCREEN_KEY}: full screen"
                                           " · O: close")
                    yield MonitorTerminal(id="b-monitor")
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
        row("monitor", cfg.monitor or "not set (o asks for it)",
            "" if cfg.monitor else "bright_black")
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
        if self.monitor_config is not None:
            state = "running" if self.monitor.running else "ended"
            parts.append(f"monitor: {self.monitor_config} ({state})")
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
        elif action == "kconfig":
            self.open_in_kconfig(cfg)
        elif action == "delete_dir":
            self.delete_build_dir(cfg)
        elif action == "monitor":
            self.open_monitor(cfg)
        elif action == "close_monitor":
            self.action_close_monitor()
        elif action == "set_monitor":
            self.ask_monitor_command(cfg)

    # -- monitor --

    @property
    def monitor(self) -> MonitorTerminal:
        return self.query_one("#b-monitor", MonitorTerminal)

    def ask_monitor_command(self, cfg: BuildConfig, then_open: bool = False) -> None:
        def done(value: str | None) -> None:
            if not value:
                return
            cfg.monitor = value
            self.store.save()
            self.show_details()
            if then_open:
                self.open_monitor(cfg)
        field = next(f for f in FIELDS if f.key == "monitor")
        self.push_screen(TextEditScreen(f"Monitor command for {cfg.name}", cfg.monitor,
                                        placeholder="tio /dev/ttyACM0", hint=field.hint), done)

    def open_monitor(self, cfg: BuildConfig) -> None:
        if not cfg.monitor:
            self.ask_monitor_command(cfg, then_open=True)
            return
        mon = self.monitor
        if mon.running and self.monitor_config == cfg.name:
            mon.focus()
            return
        if mon.running:
            def answered(yes: bool) -> None:
                if yes:
                    self._start_monitor(cfg)
            self.push_screen(ConfirmScreen(
                "Switch the monitor?",
                f"The monitor is open for {self.monitor_config}. Close it and open "
                f"one for {cfg.name}?"), answered)
            return
        self._start_monitor(cfg)

    def _start_monitor(self, cfg: BuildConfig) -> None:
        self.add_class("-monitor")
        mon = self.monitor
        panel = self.query_one("#b-monitor-panel", Panel)
        panel.border_title = f"Monitor · {cfg.name}"
        self.monitor_config = cfg.name

        try:
            mon.start(cfg.monitor, self.ws.topdir, monitor_env(self.ws, cfg))
        except OSError as exc:
            self.notify(f"could not start the monitor: {exc}", severity="error")
            return
        mon.focus()
        self.update_status()

    def action_close_monitor(self) -> None:
        mon = self.monitor
        if self.monitor_config is None:
            self.notify("no monitor is open")
            return
        mon.stop()
        if self.screen.maximized is not None:
            self.screen.minimize()
        self.remove_class("-monitor")
        self.monitor_config = None
        self.configs_list.focus()
        self.update_status()

    def action_monitor_fullscreen(self) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()
            return
        if self.monitor_config is None:
            self.notify("no monitor is open (o opens one)")
            return
        self.monitor.focus()
        self.screen.maximize(self.query_one("#b-monitor-panel", Panel))

    @on(MonitorTerminal.Leave)
    def _monitor_leave(self) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()
        self.configs_list.focus()

    @on(MonitorTerminal.ToggleFullscreen)
    def _monitor_fullscreen(self) -> None:
        self.action_monitor_fullscreen()

    @on(MonitorTerminal.Exited)
    def _monitor_exited(self, event: MonitorTerminal.Exited) -> None:
        if self.monitor_config is not None:
            self.update_status()

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

    def open_in_kconfig(self, cfg: BuildConfig) -> None:
        d = dts_build_dir(build_dir(self.ws, cfg))
        if not (d / "build_info.yml").is_file():
            self.notify("build it first: the Kconfig tab reads the build directory",
                        severity="warning")
            return
        opener = getattr(self.app, "open_in_kconfig", None)
        if opener is None:
            self.notify("no Kconfig tab here", severity="warning")
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
