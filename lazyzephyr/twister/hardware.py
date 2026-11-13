"""Hardware settings for the Tests tab: run on a board, its console UART and flasher."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, SelectionList, Static
from textual.widgets.option_list import Option

from ..builder import meta
from ..builder.app import PickerScreen, TextEditScreen
from .jobs import Hardware

OTHER = "\0other"  # picker item: type a value instead

ROWS = (
    ("enabled", "Run on the board"),
    ("serial", "Serial port"),
    ("baud", "Baud rate"),
    ("runner", "Flash runner"),
    ("dev_id", "Debug probe"),
    ("fixtures", "Fixtures"),
)


def platform_runners(cwd: Path, platform: str) -> meta.Runners | None:
    """The flash runners a twister platform's board supports, if the board is found."""
    from ..dts.workspace import find_board, find_workspace

    try:
        return meta.board_runners(find_board(find_workspace(cwd), platform))
    except (LookupError, OSError):
        return None


class FixtureScreen(ModalScreen[list[str] | None]):
    """Tick the fixtures the board has, from the ones the tests need, or type others."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter,ctrl+s", "apply", "Apply", priority=True),
    ]

    def __init__(self, known: dict[str, int], selected: list[str]):
        super().__init__()
        self.known, self.selected = known, selected

    def compose(self) -> ComposeResult:
        muted = "bright_black"
        items = []
        for name, count in self.known.items():
            label = Text.assemble(f"{name:<24}", (f"{count} test{'s' * (count != 1)}", muted))
            items.append((label, name, name in self.selected))
        for name in self.selected:
            if name not in self.known:
                items.append((Text.assemble(f"{name:<24}", ("no test needs it", muted)),
                              name, True))
        with Vertical(id="fixture-dialog", classes="panel dialog") as dialog:
            dialog.border_title = "Fixtures"
            dialog.border_subtitle = "space: toggle · enter: apply · esc: cancel"
            yield Static("twister -X: what the board has attached. Tests that need a "
                         "fixture you don't tick are skipped.", classes="hint")
            if items:
                yield SelectionList[str](*items, id="fixture-list")
            else:
                yield Static("No test asks for a fixture (harness_config: fixture).",
                             classes="hint")
            yield Input(placeholder="other fixtures, comma separated", id="fixture-extra")

    def on_mount(self) -> None:
        lists = self.query(SelectionList)
        (lists.first() if lists else self.query_one(Input)).focus()

    def action_apply(self) -> None:
        lists = self.query(SelectionList)
        chosen = list(lists.first().selected) if lists else []
        extra = self.query_one("#fixture-extra", Input).value
        chosen += [f.strip() for f in extra.split(",") if f.strip()]
        self.dismiss(list(dict.fromkeys(chosen)))

    def action_cancel(self) -> None:
        self.dismiss(None)


class HardwareScreen(ModalScreen[Hardware | None]):
    """Edit a copy of the hardware settings; ctrl+s applies them."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "apply", "Apply", priority=True),
    ]

    def __init__(self, hardware: Hardware, platforms: list[str], cwd: Path,
                 fixtures: dict[str, int] | None = None):
        super().__init__()
        self.draft = replace(hardware, fixtures=list(hardware.fixtures))
        self.platforms, self.cwd = platforms, cwd
        self.known_fixtures = fixtures or {}  # fixture -> how many scenarios need it
        self.runners: meta.Runners | None = None
        self.runners_loading = len(platforms) == 1

    def compose(self) -> ComposeResult:
        with Vertical(id="hardware-dialog", classes="panel dialog") as dialog:
            dialog.border_title = "Hardware"
            dialog.border_subtitle = "enter: change · ctrl+s: apply · esc: cancel"
            yield Static(self._platform_hint(), classes="hint")
            yield OptionList(id="hardware-rows")
            yield Static("Twister flashes with west flash and reads the console from the "
                         "serial port. Fixtures (-X) also apply without a board.",
                         classes="hint")

    def on_mount(self) -> None:
        self.refill()
        self.query_one(OptionList).focus()
        if self.runners_loading:
            self._load_runners(self.platforms[0])

    @work(thread=True, exclusive=True)
    def _load_runners(self, platform: str) -> None:
        runners = platform_runners(self.cwd, platform)
        self.app.call_from_thread(self._runners_loaded, runners)

    def _runners_loaded(self, runners: meta.Runners | None) -> None:
        self.runners, self.runners_loading = runners, False
        self.refill()

    def _platform_hint(self) -> Text:
        if len(self.platforms) == 1:
            return Text(f"platform: {self.platforms[0]}")
        return Text("pick exactly one platform with p to run on a board", style="yellow")

    def _value(self, key: str) -> Text:
        d = self.draft
        muted = "bright_black"
        if key == "enabled":
            return Text("on", style="green") if d.enabled else Text("off", style=muted)
        if key == "serial":
            return Text(d.serial) if d.serial else Text("none", style=muted)
        if key == "baud":
            return Text(str(d.baud)) if d.baud else Text("115200 (twister default)", style=muted)
        if key == "runner":
            if d.runner:
                return Text(d.runner)
            default = self.runners.flash_default if self.runners else None
            return Text(f"board default{f' ({default})' if default else ''}", style=muted)
        if key == "dev_id":
            return Text(d.dev_id) if d.dev_id else Text("none (runner picks)", style=muted)
        return Text(", ".join(d.fixtures)) if d.fixtures else Text("none", style=muted)

    def refill(self) -> None:
        rows = self.query_one(OptionList)
        highlighted = rows.highlighted
        rows.clear_options()
        for key, label in ROWS:
            prompt = Text(f"{label:<18}", style="bold")
            prompt.append_text(self._value(key))
            rows.add_option(Option(prompt, id=key))
        rows.highlighted = highlighted if highlighted is not None else 0

    @on(OptionList.OptionSelected, "#hardware-rows")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        getattr(self, f"_edit_{event.option.id}")()

    def _set(self, **changes) -> None:
        self.draft = replace(self.draft, **changes)
        self.refill()

    def _pick(self, title: str, items: list, key: str, hint: str = "",
              placeholder: str = "") -> None:
        """One pick from `items` (id, label, search); "other…" types a value."""
        items = [*items, (OTHER, Text("other… (type it)", style="bright_black"), "other")]

        def picked(result: list[str] | None) -> None:
            if not result:
                return
            if result[0] == OTHER:
                def typed(value: str | None) -> None:
                    if value is not None:
                        self._set(**{key: value or None})

                current = getattr(self.draft, key) or ""
                self.app.push_screen(TextEditScreen(title, current, placeholder=placeholder),
                                     typed)
            else:
                self._set(**{key: result[0] or None})

        self.app.push_screen(PickerScreen(title, items, hint=hint), picked)

    def _edit_enabled(self) -> None:
        self._set(enabled=not self.draft.enabled)

    def _edit_serial(self) -> None:
        ports = meta.find_serial_ports()
        items = [("", Text("none", style="bright_black"), "none")]
        items += [(p.path, Text.assemble(p.path, (f"  → {p.device}", "bright_black")
                                         if p.device != p.path else ""),
                   f"{p.path} {p.device}") for p in ports]
        hint = "" if ports else "no USB serial ports found"
        self._pick("Serial port (console UART)", items, "serial", hint, "/dev/ttyACM0")

    def _edit_baud(self) -> None:
        def done(value: str | None) -> None:
            if value is None:
                return
            if value and (not value.isdigit() or int(value) < 1):
                self.notify("baud rate must be a positive number", severity="error")
                return
            self._set(baud=int(value) if value else None)

        self.app.push_screen(TextEditScreen("Baud rate", str(self.draft.baud or ""),
                                            placeholder="115200",
                                            hint="empty = twister's default (115200)"), done)

    def _edit_runner(self) -> None:
        runners = self.runners
        default = runners.flash_default if runners else None
        items = [("", Text("board default" + (f" ({default})" if default else ""),
                           style="bright_black"), "default")]
        if runners:
            items += [(n, n + ("  (default)" if n == default else ""), n) for n in runners.names]
        if self.runners_loading:
            hint = "still reading the board's runners…"
        elif runners is None:
            hint = "board not found: pick one platform with p, or type a runner"
        else:
            hint = "from the board's board.cmake"
        self._pick("Flash runner", items, "runner", hint, "jlink")

    def _edit_dev_id(self) -> None:
        probes = meta.find_probes()
        items = [("", Text("none (runner picks)", style="bright_black"), "none")]
        items += [(p.dev_id, Text.assemble(p.dev_id, (f"  {p.kind} · {p.product}",
                                                      "bright_black")),
                   f"{p.dev_id} {p.kind} {p.product}") for p in probes]
        hint = "" if probes else "no debug probes found on USB"
        self._pick("Debug probe", items, "dev_id", hint, "serial number")

    def _edit_fixtures(self) -> None:
        def done(fixtures: list[str] | None) -> None:
            if fixtures is not None:
                self._set(fixtures=fixtures)

        self.app.push_screen(FixtureScreen(self.known_fixtures, self.draft.fixtures), done)

    def action_apply(self) -> None:
        self.dismiss(self.draft)

    def action_cancel(self) -> None:
        self.dismiss(None)
