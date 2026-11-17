"""The SDK tab: install the SDK the workspace's Zephyr needs, with Nix or west sdk."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from ..builder.app import BUILD_CSS, ConfirmScreen, Panel, ScrollPanel
from ..builder.runner import Job, Runner, west_command
from ..clipboard import SystemClipboard
from ..dts.workspace import Workspace
from ..view import COMMON_CSS, ForwardActions, View
from . import backends, pyenv
from .backends import Availability
from .model import (
    BACKENDS,
    SETTINGS_FILE,
    InstalledSdk,
    Settings,
    active_sdk,
    elsewhere,
    hosttools_path,
    required_version,
    sdk_path,
    store_dir,
    store_sdks,
)

HOSTTOOLS = "hosttools"
BACKEND_LABELS = {"west": "west sdk", "nix": "Nix (zephyr-nix)"}


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "lazyzephyr"


def home(path: Path | str) -> str:
    text = str(path)
    h = str(Path.home())
    return "~" + text[len(h) :] if text == h or text.startswith(h + "/") else text


class BackendList(ForwardActions, OptionList):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]


class ToolchainList(ForwardActions, OptionList):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("space", "toggle_toolchain", "Toggle"),
        Binding("a", "toggle_all", "All/none"),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape,q,question_mark", "close", "Close")]

    TEXT = """\
The SDK tab installs the Zephyr SDK (the toolchains and host tools) that this
workspace's Zephyr asks for in its SDK_VERSION file.

[b]Install with[/b]
  enter              use this way of installing (remembered)
  west sdk           Zephyr's own installer (Zephyr 4.0 or later), the default
  Nix                builds it with nix-community/zephyr-nix (needs nix)
[b]Toolchains[/b]
  space              add or remove a toolchain (or the host tools)
  a                  all or none
  [green]+[/green] to install · [red]-[/red] to remove · ✔ installed
[b]Anywhere in the tab[/b]
  g                  the setup steps: what this workspace still needs
  p                  set up the workspace's Python environment: a .venv with
                     west and Zephyr's requirements (again when they change)
  i                  install: apply the changes in the list
  D                  remove this SDK version from lazyzephyr's folder
  u                  builds use the .venv and this SDK: on/off
  r                  look again (installed SDKs, toolchain list)
  x                  stop the running install
  L                  clear the output
  z                  maximize the focused panel
  ?                  this help

SDKs are kept in one folder shared by every workspace, one
zephyr-sdk-<version> per version (set LAZYZEPHYR_SDK_DIR to move it). Builds
and twister runs started by lazyzephyr use the version this Zephyr needs and
the workspace's .venv, unless you set ZEPHYR_SDK_INSTALL_DIR or activated a
virtualenv yourself.
"""

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="sdk-help-dialog", classes="panel dialog") as dialog:
            dialog.border_title = "SDK help"
            yield Static(self.TEXT)

    def action_close(self) -> None:
        self.dismiss(None)


class SetupScreen(ModalScreen[str | None]):
    """The setup steps, and what's done: "next" does the next one, "never" opts out."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "answer('next')", "Next step"),
        Binding("escape,q", "answer(None)", "Not now"),
        Binding("n", "answer('never')", "Don't show again"),
    ]

    def __init__(self, steps: list[tuple[bool, str, str]], next_step: str | None):
        super().__init__()
        self.steps, self.next_step = steps, next_step

    def compose(self) -> ComposeResult:
        t = Text()
        if self.next_step is None:
            t.append("This workspace is set up for building.\n\n", style="green")
        else:
            t.append("Before this workspace can build, it needs:\n\n")
        for n, (done, title, detail) in enumerate(self.steps, 1):
            t.append(" ✔ " if done else " ○ ", style="green" if done else "yellow")
            t.append(f"{n}  {title}\n", style="bold" if not done else "")
            t.append(f"      {detail}\n", style="bright_black")
        t.append("\n")
        keys = [("enter", self.next_step or "close")]
        if self.next_step is not None:
            keys.append(("esc", "not now"))
        keys.append(("n", "don't show this again (g shows it)"))
        for key, what in keys:
            t.append(f" {key:<7}", style="bold")
            t.append(what + "\n")
        with Vertical(id="sdk-setup-dialog", classes="panel dialog") as dialog:
            dialog.border_title = "Set up"
            yield Static(t)

    def action_answer(self, value: str | None) -> None:
        if value == "next" and self.next_step is None:
            value = None
        self.dismiss(value)


SDK_CSS = """
    SdkView #s-main { layout: horizontal; height: 1fr; }
    SdkView #s-left { width: 50%; min-width: 40; }
    SdkView #s-right { width: 1fr; }
    SdkView #s-status-panel { height: 2fr; padding: 0 1; }
    SdkView #s-backend-panel { height: 6; }
    SdkView #s-backends, SdkView #s-toolchains {
        height: 1fr; border: none; background: $surface; }
    SdkView #s-toolchains-panel { height: 1fr; }
    SdkView #s-log { height: 1fr; }
    SdkView #s-statusbar { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    SdkView.-portrait #s-main { layout: vertical; }
    SdkView.-portrait #s-left, SdkView.-portrait #s-right { width: 100%; min-width: 0; }
    #sdk-help-dialog { width: 84; height: 80%; }
    #sdk-setup-dialog { width: 80; height: auto; max-height: 90%; padding: 0 1; }
    HelpScreen, SetupScreen { align: center middle; }
"""


class SdkView(View):
    SPLITS = ("s-main", "s-left", "s-right")
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("i", "install", "Install"),
        Binding("p", "setup_python", "Python env"),
        Binding("g", "guide", "Setup"),
        Binding("D", "remove_sdk", "Remove SDK", show=False),
        Binding("u", "toggle_use", "Use for builds", show=False),
        Binding("r", "rescan", "Refresh", show=False),
        Binding("x", "cancel_job", "Stop"),
        Binding("L", "clear_log", "Clear output", show=False),
        Binding("question_mark", "help", "Help"),
        Binding("z", "maximize", "Zoom", show=False),
    ]

    def __init__(
        self,
        ws: Workspace | None,
        store: Path | None = None,
        west: list[str] | None = None,
        nix: list[str] | None = None,
        python: str | None = None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.ws = ws
        self.zephyr_base = ws.zephyr_base if ws else None
        self.topdir = ws.topdir if ws else None
        self.python = python  # creates the .venv (default: lazyzephyr's Python)
        self.pyenv: pyenv.PyEnv | None = None
        self.store = store or store_dir()
        self.west, self.nix = west, nix
        self.settings = Settings.load(self.store)
        self.version = required_version(self.zephyr_base)
        self.runner = Runner(
            self.topdir or self.store, self._on_line, self._on_change, west, topdir=self.topdir
        )
        self.job: Job | None = None
        self.availability: dict[str, Availability] = {}
        self.available: list[str] | None = None  # toolchains in the SDK release
        self.release_error: str | None = None
        self.nix_versions: dict[str, str] | None = None
        self.nix_error: str | None = None
        self.nix_busy = False
        self.installed: InstalledSdk | None = None  # this version, in our store
        self.others: list[InstalledSdk] = []
        self.selected: set[str] = set()
        self.hosttools = self.settings.hosttools
        self._rows: list[str] = []
        self._loaded = False
        self._guided = False  # the setup steps were offered this session
        self._guide_waiting = False  # offer the steps once own_missing is known
        self.own_west: str | None = None  # a west of the user's own (Nix shell, ...)
        # Zephyr's base packages its Python lacks; None: not known (yet)
        self.own_missing: list[str] | None = None
        self._own_checked: str | None = None  # the west own_missing is for

    # -- layout --

    def compose(self) -> ComposeResult:
        with Horizontal(id="s-main"):
            with Vertical(id="s-left"):
                with ScrollPanel(id="s-status-panel", classes="panel") as status:
                    status.border_title = "SDK"
                    yield Static("", id="s-status")
                with Panel(id="s-backend-panel", classes="panel") as backend:
                    backend.border_title = "Install with"
                    yield BackendList(id="s-backends")
            with Vertical(id="s-right"):
                with Panel(id="s-toolchains-panel", classes="panel") as tcs:
                    tcs.border_title = "Toolchains"
                    tcs.border_subtitle = "space: toggle · i: install"
                    yield ToolchainList(id="s-toolchains")
                log = RichLog(
                    id="s-log",
                    classes="panel",
                    markup=False,
                    highlight=False,
                    wrap=False,
                    max_lines=20000,
                )
                log.border_title = "Output"
                yield log
        yield Static("", id="s-statusbar")

    @property
    def toolchain_list(self) -> ToolchainList:
        return self.query_one("#s-toolchains", ToolchainList)

    @property
    def backend_list(self) -> BackendList:
        return self.query_one("#s-backends", BackendList)

    def on_mount(self) -> None:
        self.rescan()

    def activated(self) -> None:
        # GitHub and zephyr-nix are only asked once the tab is opened.
        if not self._loaded:
            self._loaded = True
            self.load_release()
            if self.settings.backend == "nix":
                self.load_nix_versions()
        if self.focused is None or self not in self.focused.ancestors:
            self.toolchain_list.focus()
        if not self._guided:
            self._guided = True
            if self.own_west and self._own_checked != self.own_west:
                self._guide_waiting = True
            else:
                self.offer_guide()

    # -- state --

    def rescan(self) -> None:
        self.availability = {
            "nix": backends.nix_available(self.nix, self.flake),
            "west": backends.west_available(
                self.zephyr_base, self.west or west_command(self.topdir)
            ),
        }
        self.pyenv = pyenv.inspect(self.topdir, self.zephyr_base) if self.topdir else None
        self.own_west = self._own_west()
        if self.own_west and self.own_west != self._own_checked:
            self.own_missing = None
            self.check_own_west(self.own_west)
        sdks = store_sdks(self.store)
        self.installed = next((s for s in sdks if s.version == self.version), None)
        self.others = [s for s in sdks if s is not self.installed] + elsewhere(self.store)
        self.selected = set(self.installed.toolchains) if self.installed else set()
        if self.installed is None:
            self.selected = set(self.settings.toolchains)
        if self.available is not None:
            self.selected &= set(self.available) | set(self._installed_toolchains())
        self.refresh_all()

    def _installed_toolchains(self) -> list[str]:
        return self.installed.toolchains if self.installed else []

    def _hosttools_installed(self) -> bool:
        if self.installed is None:
            return False
        if self.installed.hosttools:
            return True
        return hosttools_path(self.store, self.installed.version).exists()

    @work(thread=True, exclusive=True, group="release")
    def load_release(self) -> None:
        if self.version is None:
            return
        try:
            names = backends.release_assets(self.version, cache_dir())
            available, error = backends.toolchains_in_release(names, self.version), None
            if not available:
                error = f"SDK {self.version} has no toolchains for this computer"
        except LookupError as exc:
            available, error = None, str(exc)
        self.call_from_thread(self._release_loaded, available, error)

    def _release_loaded(self, available: list[str] | None, error: str | None) -> None:
        self.available, self.release_error = available, error
        if available is not None:
            self.selected &= set(available) | set(self._installed_toolchains())
        self.refresh_all()

    @work(thread=True, exclusive=True, group="nix")
    def load_nix_versions(self) -> None:
        self.nix_busy = True
        self.call_from_thread(self.refresh_all)
        command = backends.nix_versions_command(self.flake)
        if self.nix:
            command = [*self.nix, *command[1:]]
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
            if proc.returncode != 0:
                lines = proc.stderr.strip().splitlines()
                raise LookupError(lines[-1] if lines else f"nix exited with {proc.returncode}")
            versions = json.loads(proc.stdout)
            if not isinstance(versions, dict):
                raise LookupError("unexpected answer from nix eval")
            result, error = {str(k): str(v) for k, v in versions.items()}, None
        except (OSError, ValueError, LookupError, subprocess.TimeoutExpired) as exc:
            result, error = None, f"could not ask zephyr-nix for its SDKs: {exc}"
        self.call_from_thread(self._nix_loaded, result, error)

    def _nix_loaded(self, versions: dict[str, str] | None, error: str | None) -> None:
        self.nix_versions, self.nix_error, self.nix_busy = versions, error, False
        self.refresh_all()

    # -- the plan --

    def changes(self) -> tuple[list[str], list[str]]:
        """What to install and to remove: toolchains, and HOSTTOOLS for the host tools."""
        have = set(self._installed_toolchains())
        add, remove = sorted(self.selected - have), sorted(have - self.selected)
        if self.installed is not None or self.selected:
            if self.hosttools and not self._hosttools_installed():
                add.insert(0, HOSTTOOLS)
            elif not self.hosttools and self._hosttools_installed():
                remove.insert(0, HOSTTOOLS)
        return add, remove

    @property
    def flake(self) -> str:
        return self.settings.zephyr_nix or backends.ZEPHYR_NIX

    def nix_attr(self) -> str | None:
        if self.nix_versions is None or self.version is None:
            return None
        return backends.nix_attr_for(self.nix_versions, self.version)

    def backend_problem(self, backend: str) -> str | None:
        """Why this backend can't install the SDK now, or None."""
        avail = self.availability.get(backend)
        if avail is None or not avail.ok:
            return avail.reason if avail else "unavailable"
        if backend == "nix":
            if self.nix_busy:
                return "still asking zephyr-nix which SDK versions it has…"
            if self.nix_error:
                return self.nix_error
            if self.nix_versions is not None and self.nix_attr() is None:
                have = ", ".join(sorted(set(self.nix_versions.values()))) or "none"
                return (
                    f"zephyr-nix has no SDK {self.version} (it has {have}). Use west sdk, "
                    f"or set zephyr_nix in {home(self.store / SETTINGS_FILE)} to a newer "
                    f"zephyr-nix (now {backends.short_flake(self.flake)})"
                )
            if self.nix_versions is None:
                return "zephyr-nix's SDK versions aren't known yet (r: look again)"
        return None

    # -- the setup steps --

    def _own_west(self) -> str | None:
        """west on the PATH from the user's own setup (a Nix shell, say), not our .venv."""
        west = shutil.which("west")
        if west is None:
            return None
        if self.pyenv is not None and Path(west).resolve().is_relative_to(
            self.pyenv.path.resolve()
        ):
            return None
        return west

    @work(thread=True, exclusive=True, group="own-west")
    def check_own_west(self, west: str) -> None:
        names = pyenv.base_requirements(self.zephyr_base)
        python = pyenv.python_beside(west)
        missing = pyenv.missing_requirements(python, names) if names and python else None
        self.call_from_thread(self._own_west_checked, west, missing)

    def _own_west_checked(self, west: str, missing: list[str] | None) -> None:
        self.own_missing, self._own_checked = missing, west
        self.refresh_all()
        if self._guide_waiting:  # the tab opened before this was known
            self._guide_waiting = False
            self.offer_guide()

    def python_ready(self) -> bool:
        active = os.environ.get("VIRTUAL_ENV")
        if active and self.pyenv and Path(active).resolve() != self.pyenv.path.resolve():
            return True  # the user's own virtualenv
        if self.pyenv is not None and not self.pyenv.exists and self.own_west:
            return not self.own_missing  # west of their own, with Zephyr's packages
        return self.pyenv is not None and self.pyenv.ready

    def sdk_ready(self) -> bool:
        if self.installed is not None:
            return True
        return any(o.version == self.version for o in self.others)

    def steps(self) -> list[tuple[str, bool, str, str]]:
        """(key, done, title, detail) for each thing the workspace needs."""
        if self.topdir is None or self.version is None:
            return []
        venv = home(self.pyenv.path) if self.pyenv else ".venv"
        python = f"{venv} with west and Zephyr's Python packages (p)"
        if self.own_west and self.pyenv is not None and not self.pyenv.exists:
            if self.own_missing:
                python = (
                    f"your own west lacks {len(self.own_missing)} of Zephyr's packages: "
                    f"p sets up {venv} with all of them"
                )
            else:
                python = f"your own west ({home(self.own_west)}); p sets up {venv} instead"
        installer = BACKEND_LABELS[self.settings.backend]
        how = self.backend_problem(self.settings.backend) or (
            f"toolchains and host tools, with {installer} (space, then i)"
        )
        return [
            (
                "python",
                self.python_ready(),
                "Python environment",
                python,
            ),
            (
                "sdk",
                self.sdk_ready(),
                f"Zephyr SDK {self.version}",
                how,
            ),
        ]

    def offer_guide(self, force: bool = False) -> None:
        """Show the setup steps if something is missing (always when forced)."""
        steps = self.steps()
        todo = [k for k, done, _, _ in steps if not done]
        if not steps or (not force and (not todo or not self.settings.guide)):
            return
        if isinstance(self.app.screen, ModalScreen):
            return
        labels = {"python": "set up the Python environment", "sdk": "choose the toolchains"}
        screen = SetupScreen(
            [(done, title, detail) for _, done, title, detail in steps],
            labels[todo[0]] if todo else None,
        )
        self.app.push_screen(screen, lambda answer: self._guide_answered(answer, todo))

    def _guide_answered(self, answer: str | None, todo: list[str]) -> None:
        if answer == "never":
            self.settings.guide = False
            self.settings.save(self.store)
            self.notify("g shows the setup steps again")
        elif answer == "next" and todo[0] == "python":
            self.action_setup_python()
        elif answer == "next":
            self.toolchain_list.focus()
            if not self.selected:
                self.notify("tick the toolchains your boards need with space, then press i")

    def action_guide(self) -> None:
        self.offer_guide(force=True)

    # -- rendering --

    def refresh_all(self) -> None:
        self.show_status()
        self.fill_backends()
        self.fill_toolchains()
        self.update_statusbar()

    def show_status(self) -> None:
        t = Text()

        def row(label: str, value, style: str = "", mark: bool | None = None) -> None:
            if mark is None:
                t.append("  ")
            else:
                t.append("✔ " if mark else "✗ ", style="green" if mark else "yellow")
            t.append(f"{label:<11}", style="bold")
            t.append_text(value if isinstance(value, Text) else Text(str(value), style=style))
            t.append("\n")

        def note(text: str) -> None:
            t.append(" " * 13 + text + "\n", style="bright_black")

        if self.zephyr_base is None:
            row("Zephyr", "not found: run inside a west workspace or set ZEPHYR_BASE", "red")
        else:
            row("Zephyr", home(self.zephyr_base))
            if self.version is None:
                note("no SDK_VERSION file: the SDK version it needs is unknown")
            else:
                note(f"needs Zephyr SDK {self.version}")
        t.append("\n")
        self._python_rows(row)
        if self.installed:
            what = Text(f"{self.version} installed with {self.installed.backend}", style="green")
            n = len(self.installed.toolchains)
            what.append(f" · {n} toolchain{'s' if n != 1 else ''}")
            if self._hosttools_installed():
                what.append(" · host tools")
            row("SDK", what, mark=True)
        elif self.version:
            other = next((o for o in self.others if o.version == self.version), None)
            if other is not None:
                row("SDK", f"{self.version} in {home(other.path)}", "green", mark=True)
            else:
                row(
                    "SDK", f"{self.version} not installed: tick toolchains, then i", "yellow", False
                )
        t.append("\n")
        path, reason = active_sdk(self.zephyr_base, self.store)
        row("Builds use", home(path) if path else reason, "green" if path else "bright_black")
        if path:
            note(reason)
        note(f"u: {'stop using' if self.settings.use else 'use'} the .venv and SDK from here")
        row("SDKs in", home(self.store))
        note("shared by every workspace")
        problem = self.backend_problem(self.settings.backend) if self.version else None
        if problem:
            t.append("\n" + problem + "\n", style="yellow")
        if self.release_error:
            t.append("\n" + self.release_error + "\n", style="red")
        if self.others:
            t.append("\nOther SDKs\n", style="bold")
            for sdk in self.others:
                t.append(f"  {sdk.version:<8}", style="cyan")
                t.append(f"{sdk.backend:<6}", style="bright_black")
                t.append(home(sdk.path) + "\n")
        self.query_one("#s-status", Static).update(t)

    def _python_rows(self, row) -> None:
        env = self.pyenv
        if env is None:
            return
        active = os.environ.get("VIRTUAL_ENV")
        if active and Path(active).resolve() != env.path.resolve():
            row("Python", f"your virtualenv {home(active)} (active, so it's used)", "green", True)
            return
        where = home(env.path)
        if not env.exists and self.own_west:
            where = home(self.own_west)
            if self.own_missing:
                shown = ", ".join(self.own_missing[:4])
                more = f" and {len(self.own_missing) - 4} more" if len(self.own_missing) > 4 else ""
                row("Python", f"your own west ({where}) lacks {shown}{more}", "yellow", False)
                row("", "p sets up a workspace .venv with all of Zephyr's packages", "yellow")
            elif self.own_missing == []:
                row("Python", f"your own west ({where}), with Zephyr's packages", "green", True)
            else:
                row("Python", f"your own west ({where})", "green", True)
                row("", "p sets up a workspace .venv with all of Zephyr's packages instead")
        elif not env.exists:
            row("Python", "no .venv in the workspace: p sets it up", "yellow", False)
        elif not env.python_ok:
            version = ".".join(map(str, env.python))
            row(
                "Python",
                f"{where} has Python {version}; Zephyr needs 3.12 or later. Delete it, then p",
                "red",
                False,
            )
        elif not env.ours:
            row(
                "Python",
                f"{where} (not set up by lazyzephyr): p installs west and Zephyr's "
                "requirements into it",
                "yellow",
                env.ready,
            )
        elif not env.has_west:
            row("Python", f"{where} has no west: p sets it up again", "yellow", False)
        elif not env.up_to_date:
            row(
                "Python", f"{where}: Zephyr's requirements changed, p updates them", "yellow", False
            )
        else:
            version = ".".join(map(str, env.python)) if env.python else "?"
            row("Python", f"{where} · Python {version}", "green", True)

    def action_setup_python(self) -> None:
        if self._busy():
            return
        if self.topdir is None or self.zephyr_base is None:
            self.notify("no west workspace to set up", severity="error")
            return
        env = pyenv.inspect(self.topdir, self.zephyr_base)
        if env.exists and not env.python_ok:
            self.notify(
                f"{home(env.path)} has a Python older than 3.12: delete it first",
                severity="error",
            )
            return
        if env.exists and not env.ours:
            self._confirm(
                "Use the existing .venv?",
                f"{home(env.path)} already exists.\n"
                "Install west and Zephyr's Python requirements into it?",
                self._setup_python,
            )
            return
        self._setup_python()

    def _setup_python(self) -> None:
        commands = pyenv.setup_commands(self.topdir, self.zephyr_base, self.python)
        job = Job("set up the Python environment", "python", "python", commands, program=[])
        self.job = job
        self.run_job(job)

    def fill_backends(self) -> None:
        lst = self.backend_list
        highlighted = lst.highlighted
        lst.clear_options()
        for name in BACKENDS:
            avail = self.availability.get(name)
            chosen = self.settings.backend == name
            label = Text()
            label.append("● " if chosen else "○ ", style="green" if chosen else "bright_black")
            label.append(BACKEND_LABELS[name], style="bold" if avail and avail.ok else "dim")
            if avail is not None:
                label.append("  " + avail.reason, style="bright_black" if avail.ok else "red")
            lst.add_option(Option(label, id=name))
        lst.highlighted = highlighted if highlighted is not None else 0

    def _label(self, name: str, installed: bool, selected: bool) -> Text:
        t = Text()
        if selected and installed:
            t.append("[✔] ", style="green")
        elif selected:
            t.append("[+] ", style="bold green")
        elif installed:
            t.append("[-] ", style="bold red")
        else:
            t.append("[ ] ", style="bright_black")
        if name == HOSTTOOLS:
            t.append("host tools", style="bold")
            t.append("  dtc, qemu, openocd, …", style="bright_black")
        else:
            t.append(name)
        if selected and not installed:
            t.append("  to install", style="green")
        elif installed and not selected:
            t.append("  to remove", style="red")
        return t

    def fill_toolchains(self) -> None:
        lst = self.toolchain_list
        highlighted = lst.highlighted
        lst.clear_options()
        self._rows = []
        if self.version is None:
            lst.add_option(Option(Text("no SDK version to install", style="bright_black")))
            return
        names = sorted(set(self.available or []) | set(self._installed_toolchains()))
        if not names:
            note = "loading the toolchain list…" if self.release_error is None else "unavailable"
            lst.add_option(Option(Text(note, style="bright_black"), disabled=True))
            return
        self._rows.append(HOSTTOOLS)
        lst.add_option(
            Option(self._label(HOSTTOOLS, self._hosttools_installed(), self.hosttools), id="ht")
        )
        have = set(self._installed_toolchains())
        for name in names:
            self._rows.append(name)
            lst.add_option(Option(self._label(name, name in have, name in self.selected), id=name))
        if highlighted is not None and highlighted < len(self._rows):
            lst.highlighted = highlighted
        else:
            lst.highlighted = 0

    def update_statusbar(self) -> None:
        add, remove = self.changes()
        parts = [f"SDK {self.version}" if self.version else "no SDK version"]
        parts.append(BACKEND_LABELS[self.settings.backend])
        parts.append(f"{len(self.selected)} toolchains")
        if add or remove:
            parts.append(f"+{len(add)} -{len(remove)}: i to install")
        if self.job is not None and self.job.status == "running":
            parts.append("installing… (x: stop)")
        self.query_one("#s-statusbar", Static).update(" · ".join(parts))

    # -- actions --

    @on(OptionList.OptionSelected, "#s-backends")
    def _backend_picked(self, event: OptionList.OptionSelected) -> None:
        name = event.option.id
        avail = self.availability.get(name)
        if avail is not None and not avail.ok:
            self.notify(avail.reason, severity="warning")
            return
        self.settings.backend = name
        self.settings.save(self.store)
        if name == "nix" and self.nix_versions is None and not self.nix_busy:
            self.load_nix_versions()
        self.refresh_all()

    @on(OptionList.OptionSelected, "#s-toolchains")
    def _toolchain_picked(self) -> None:
        self.action_toggle_toolchain()

    def action_toggle_toolchain(self) -> None:
        lst = self.toolchain_list
        if lst.highlighted is None or lst.highlighted >= len(self._rows):
            return
        name = self._rows[lst.highlighted]
        if name == HOSTTOOLS:
            self.hosttools = not self.hosttools
        else:
            self.selected ^= {name}
        self.fill_toolchains()
        self.update_statusbar()

    def action_toggle_all(self) -> None:
        names = set(self._rows) - {HOSTTOOLS}
        self.selected = set() if names <= self.selected else names
        self.fill_toolchains()
        self.update_statusbar()

    def action_toggle_use(self) -> None:
        self.settings.use = not self.settings.use
        self.settings.save(self.store)
        self.show_status()

    def action_rescan(self) -> None:
        self.rescan()
        if self.available is None:
            self.load_release()
        if self.settings.backend == "nix" and not self.nix_busy:
            self.load_nix_versions()

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def action_maximize(self) -> None:
        panel = self._panel_around(self.focused) if self.focused else None
        if panel is not None:
            self.toggle_maximize(panel)

    def action_clear_log(self) -> None:
        self.query_one("#s-log", RichLog).clear()

    def action_cancel_job(self) -> None:
        if not self.runner.cancel():
            self.notify("nothing is running")

    def _busy(self) -> bool:
        if self.runner.busy:
            self.notify("an install is already running (x: stop it)", severity="warning")
            return True
        return False

    def action_install(self) -> None:
        if self._busy() or self.version is None:
            return
        backend = self.settings.backend
        problem = self.backend_problem(backend)
        if problem:
            self.notify(problem, severity="error")
            return
        if self.available is None and self.installed is None:
            self.notify(
                self.release_error or "the toolchain list isn't loaded yet", severity="error"
            )
            return
        if not self.selected and self.installed is None:
            self.notify("choose at least one toolchain (space)", severity="warning")
            return
        current = sdk_path(self.store, self.version)
        if backend == "nix" and current.is_dir() and not current.is_symlink():
            self._confirm(
                "Replace the west install?",
                f"SDK {self.version} was installed with west sdk in\n{home(current)}.\n"
                "Delete it and install it with Nix instead?",
                lambda: self._replace_then_install(),
            )
            return
        if backend == "west" and current.is_symlink():
            self._confirm(
                "Replace the Nix install?",
                f"SDK {self.version} was installed with Nix.\n"
                "Remove the link to it and install it with west sdk instead?",
                lambda: self._replace_then_install(),
            )
            return
        _, remove = self.changes()
        if backend == "west" and remove and self.installed is not None:
            self._confirm(
                "Remove toolchains?",
                "Delete these from the SDK folder?\n  "
                + "\n  ".join("host tools" if r == HOSTTOOLS else r for r in remove),
                lambda: self._install(remove_first=remove),
            )
            return
        self._install()

    def _confirm(self, title: str, message: str, then) -> None:
        def answered(yes: bool | None) -> None:
            if yes:
                then()

        self.app.push_screen(ConfirmScreen(title, message), answered)

    def _replace_then_install(self) -> None:
        wanted = set(self.selected)
        backends.remove_sdk(self.store, self.version)
        self.rescan()
        self.selected = wanted  # rescan() went back to the saved choice
        self._install()

    def _install(self, remove_first: list[str] = ()) -> None:
        version, backend = self.version, self.settings.backend
        log = self.query_one("#s-log", RichLog)
        if HOSTTOOLS in remove_first and backends.remove_hosttools(self.store, version):
            log.write(Text("removed the host tools", style="yellow"))
        tools = [r for r in remove_first if r != HOSTTOOLS]
        for name in backends.remove_toolchains(self.store, version, tools):
            log.write(Text(f"removed {name}", style="yellow"))
        add, _ = self.changes()
        toolchains = sorted(self.selected)
        if backend == "west":
            hosttools = HOSTTOOLS in add
            to_install = [a for a in add if a != HOSTTOOLS] if self.installed else toolchains
            if not to_install and not hosttools:
                self._finished_settings()
                self.rescan()
                return
            args = backends.west_install_args(
                version,
                self.store,
                to_install,
                hosttools,
                backends.west_toolchain_flag(self.zephyr_base),
            )
            job = Job(f"install SDK {version}", version, "install", [args])
        else:
            commands = backends.nix_install_commands(
                self.nix_attr(), version, self.store, toolchains, self.hosttools, self.flake
            )
            if not self.hosttools:
                link = hosttools_path(self.store, version)
                if link.is_symlink():
                    link.unlink()
            program = self.nix or ["nix"]
            job = Job(
                f"install SDK {version}",
                version,
                "install",
                [c[1:] for c in commands],
                program=program,
            )
        self.store.mkdir(parents=True, exist_ok=True)
        self.job = job
        self.run_job(job)

    @work(exclusive=True, group="job")
    async def run_job(self, job: Job) -> None:
        await self.runner.run(job)

    def _finished_settings(self) -> None:
        self.settings.toolchains = sorted(self.selected)
        self.settings.hosttools = self.hosttools
        self.settings.save(self.store)

    def _on_line(self, job: Job, line: str) -> None:
        log = self.query_one("#s-log", RichLog)
        line = line.replace(str(Path.home()) + "/", "~/")
        if line.startswith("$ "):
            log.write(Text(line, style="bold cyan"))
        else:
            log.write(Text.from_ansi(line))

    def _on_change(self, job: Job) -> None:
        self.update_statusbar()
        if job.status in ("passed", "failed", "cancelled", "error"):
            log = self.query_one("#s-log", RichLog)
            style = "green" if job.status == "passed" else "red"
            log.write(Text(f"{job.title}: {job.status}", style=f"bold {style}"))
            if job.status == "passed" and job.action == "python":
                pyenv.mark_set_up(self.topdir, self.zephyr_base)
                self.rescan()
                self.offer_guide()  # on to the SDK, if that's still missing
                return
            if job.status == "passed":
                self._finished_settings()
                if not sdk_path(self.store, self.version).exists():
                    log.write(
                        Text(
                            f"west sdk used an SDK {self.version} that was already installed "
                            "elsewhere, so lazyzephyr's folder doesn't have it. Builds will "
                            "find that one themselves.",
                            style="yellow",
                        )
                    )
            self.rescan()

    def action_remove_sdk(self) -> None:
        if self._busy() or self.version is None:
            return
        path = sdk_path(self.store, self.version)
        if not path.exists() and not path.is_symlink():
            self.notify(f"lazyzephyr's folder has no SDK {self.version}")
            return
        how = "the link to the Nix build" if path.is_symlink() else "the folder"
        self._confirm(
            f"Remove SDK {self.version}?",
            f"Delete {how}:\n{home(path)}",
            self._remove_sdk,
        )

    def _remove_sdk(self) -> None:
        backends.remove_sdk(self.store, self.version)
        self.query_one("#s-log", RichLog).write(Text(f"removed SDK {self.version}", style="yellow"))
        self.rescan()


class LazySdkApp(SystemClipboard, App):
    """The SDK tab on its own (tests)."""

    CSS = COMMON_CSS + BUILD_CSS + SDK_CSS
    BINDINGS: ClassVar[list[BindingType]] = [Binding("q", "quit", "Quit")]

    def __init__(self, view: SdkView):
        super().__init__()
        self.view_ = view

    def compose(self) -> ComposeResult:
        yield self.view_
        yield Footer(compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        self.view_.activated()
