"""A serial monitor (tio, west espressif monitor, ...) in a terminal inside the build tab.

The command runs in a pseudo terminal; pyte keeps the screen and Textual draws it.
While the terminal has focus every key goes to the command, except LEAVE_KEY
(back to the configs, the command keeps running), FULLSCREEN_KEY and CLOSE_KEY,
and ctrl+c while text is selected with the mouse (it copies the text instead).
They are ctrl keys that serial consoles (Zephyr's shell, tio, west espressif
monitor) leave alone, and that get through tmux and every terminal.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import shutil
import signal
import struct
import subprocess
import termios
from collections import deque
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

import pyte
from rich.segment import Segment
from rich.style import Style
from textual import events
from textual.binding import Binding, BindingType
from textual.geometry import Size
from textual.message import Message
from textual.selection import Selection
from textual.strip import Strip
from textual.widget import Widget

LEAVE_KEY = "ctrl+o"
FULLSCREEN_KEY = "ctrl+z"
CLOSE_KEY = "ctrl+x"
OWN_KEYS = {
    LEAVE_KEY: "leave",
    FULLSCREEN_KEY: "fullscreen",
    "f11": "fullscreen",
    CLOSE_KEY: "close",
}
SCROLLBACK = 10000
DECCKM = 1 << 5  # private mode 1: application cursor keys

KEYS = {
    "enter": "\r",
    "tab": "\t",
    "shift+tab": "\x1b[Z",
    "backspace": "\x7f",
    "ctrl+h": "\x08",
    "escape": "\x1b",
    "space": " ",
    "ctrl+space": "\x00",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "insert": "\x1b[2~",
    "delete": "\x1b[3~",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "f1": "\x1bOP",
    "f2": "\x1bOQ",
    "f3": "\x1bOR",
    "f4": "\x1bOS",
    "f5": "\x1b[15~",
    "f6": "\x1b[17~",
    "f7": "\x1b[18~",
    "f8": "\x1b[19~",
    "f9": "\x1b[20~",
    "f10": "\x1b[21~",
    "f12": "\x1b[24~",
}
ARROWS = {"up": "A", "down": "B", "right": "C", "left": "D"}


def key_bytes(event: events.Key, app_cursor: bool = False) -> bytes | None:
    """What a terminal would send for this key, or None if nothing."""
    key = event.key
    if key in ARROWS:
        return ("\x1bO" if app_cursor else "\x1b[").encode() + ARROWS[key].encode()
    if key in KEYS:
        return KEYS[key].encode()
    if key.startswith("ctrl+") and len(key) == 6 and key[5].isalpha():
        return bytes([ord(key[5].lower()) - ord("a") + 1])
    if event.character:
        return event.character.encode()
    return None


@lru_cache(maxsize=1024)
def _color(name: str) -> str | None:
    if name == "default":
        return None
    if name == "brown":  # pyte's name for yellow
        return "yellow"
    if name.startswith("bright"):
        return "bright_" + _color(name[6:])
    if len(name) == 6 and all(c in "0123456789abcdefABCDEF" for c in name):
        return "#" + name
    return name


@lru_cache(maxsize=4096)
def char_style(
    fg: str,
    bg: str,
    bold: bool,
    italics: bool,
    underscore: bool,
    strikethrough: bool,
    reverse: bool,
) -> Style:
    try:
        return Style(
            color=_color(fg),
            bgcolor=_color(bg),
            bold=bold or None,
            italic=italics or None,
            underline=underscore or None,
            strike=strikethrough or None,
            reverse=reverse or None,
        )
    except Exception:  # an unknown colour name: draw it plain
        return Style()


class TermScreen(pyte.Screen):
    """A pyte screen that keeps the lines scrolled off the top, and answers the
    terminal's queries (cursor position...) back to the command."""

    def __init__(self, columns: int, lines: int, reply: Callable[[str], None]):
        self.scrollback: deque = deque(maxlen=SCROLLBACK)
        self.scrolled = 0  # lines ever scrolled off the top
        self._reply = reply
        super().__init__(columns, lines)

    def index(self) -> None:
        top, bottom = self.margins or pyte.screens.Margins(0, self.lines - 1)
        if self.cursor.y == bottom and top == 0:
            self.scrollback.append(self.buffer[0])
            self.scrolled += 1
        super().index()

    def write_process_input(self, data: str) -> None:
        self._reply(data)

    def line_at(self, row: int):
        """Row `row` of the screen; negative rows are history (-1 the newest)."""
        if row < 0:
            index = len(self.scrollback) + row
            return self.scrollback[index] if 0 <= index < len(self.scrollback) else None
        return self.buffer[row] if row < self.lines else None

    def line_text(self, row: int) -> str:
        line = self.line_at(row)
        if not line:
            return ""
        return "".join(
            (line[x].data if x in line else " ") or "" for x in range(self.columns)
        ).rstrip()


class MonitorProcess:
    """`sh -c command` on a pseudo terminal."""

    def __init__(
        self,
        command: str,
        cwd: Path,
        env: dict[str, str],
        columns: int,
        lines: int,
        on_output: Callable[[bytes], None],
        on_exit: Callable[[int], None],
    ):
        self.command = command
        self.on_output, self.on_exit = on_output, on_exit
        self.returncode: int | None = None
        self._waiters: list[Callable[[], None]] = []
        master, slave = os.openpty()
        self.fd = master
        self.resize(columns, lines)
        shell = shutil.which("sh") or "/bin/sh"
        try:
            self.proc = subprocess.Popen(
                [shell, "-c", command],
                cwd=cwd,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                preexec_fn=_take_terminal,  # noqa: PLW1509 - needs a controlling tty
                close_fds=True,
            )
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.loop = asyncio.get_running_loop()
        self.loop.add_reader(master, self._readable)

    @property
    def running(self) -> bool:
        return self.returncode is None

    def _readable(self) -> None:
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: every process on the terminal has gone
            data = b""
        if data:
            self.on_output(data)
            return
        self.loop.remove_reader(self.fd)
        self.loop.run_in_executor(None, self.proc.wait).add_done_callback(self._exited)

    def _exited(self, fut) -> None:
        self.returncode = self.proc.returncode
        self._close_fd()
        self.on_exit(self.returncode)
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            waiter()

    def when_exited(self, callback: Callable[[], None]) -> None:
        """Call `callback` once the command has gone (now, if it already has)."""
        if self.running:
            self._waiters.append(callback)
        else:
            callback()

    def devices(self) -> set[str]:
        """The /dev files (serial ports...) the command holds or names, resolved."""
        held = session_devices(self.proc.pid) if self.running else set()
        return held | command_devices(self.command)

    def _close_fd(self) -> None:
        if self.fd >= 0:
            try:
                self.loop.remove_reader(self.fd)
            except (ValueError, RuntimeError):
                pass
            os.close(self.fd)
            self.fd = -1

    def write(self, data: bytes) -> None:
        if self.fd < 0:
            return
        try:
            os.write(self.fd, data)
        except (BlockingIOError, OSError):
            pass

    def resize(self, columns: int, lines: int) -> None:
        if self.fd >= 0:
            fcntl.ioctl(
                self.fd,
                termios.TIOCSWINSZ,
                struct.pack("HHHH", max(lines, 1), max(columns, 1), 0, 0),
            )

    def close(self) -> None:
        """Hang up, like closing a terminal window; kill it if it doesn't listen."""
        if self.proc.poll() is not None:
            return
        _signal_group(self.proc.pid, signal.SIGHUP)
        _signal_group(self.proc.pid, signal.SIGCONT)
        self.loop.call_later(2, self._kill)

    def _kill(self) -> None:
        if self.proc.poll() is None:
            _signal_group(self.proc.pid, signal.SIGKILL)


def session_devices(sid: int, proc: Path = Path("/proc")) -> set[str]:
    """The /dev files open in any process of session `sid` (Linux; empty elsewhere)."""
    found: set[str] = set()
    try:
        pids = [d for d in os.listdir(proc) if d.isdigit()]
    except OSError:
        return found
    for pid in pids:
        try:
            stat = (proc / pid / "stat").read_text()
            # pid (comm) state ppid pgrp session ...; comm may hold spaces and parens.
            if int(stat.rpartition(")")[2].split()[3]) != sid:
                continue
            fds = os.listdir(proc / pid / "fd")
        except (OSError, ValueError, IndexError):
            continue
        for fd in fds:
            try:
                target = os.readlink(proc / pid / "fd" / fd)
            except OSError:
                continue
            if target.startswith("/dev/") and not target.startswith("/dev/pts/"):
                found.add(os.path.realpath(target))
    return found


_DEV_PATH = re.compile(r"/dev/[^\s'\";,]+")


def command_devices(command: str) -> set[str]:
    """The /dev paths a monitor command names, resolved (`tio /dev/ttyACM0`).

    A monitor may not hold its port all the time (tio waits for a board that
    was unplugged), but it takes it back as soon as it can.
    """
    return {os.path.realpath(m) for m in _DEV_PATH.findall(command)}


def _take_terminal() -> None:
    # In the child: make the pty our controlling terminal, so ctrl+c is SIGINT.
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class MonitorTerminal(Widget, can_focus=True):
    """Draws a TermScreen and sends keys to the process behind it."""

    DEFAULT_CSS = """
    MonitorTerminal { height: 1fr; width: 1fr; background: $surface; }
    """

    # For the footer (and clicks on it); the keys themselves are handled in _on_key.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding(LEAVE_KEY, "leave", "Back to configs"),
        Binding(FULLSCREEN_KEY, "fullscreen", "Full screen"),
        Binding(CLOSE_KEY, "close", "Close monitor"),
        Binding("ctrl+c", "copy", "Copy selection"),  # only while text is selected
    ]

    class Exited(Message):
        def __init__(self, returncode: int) -> None:
            super().__init__()
            self.returncode = returncode

    class Leave(Message):
        """LEAVE_KEY: focus something else; the monitor keeps running."""

    class ToggleFullscreen(Message):
        pass

    class Close(Message):
        """CLOSE_KEY: end the monitor."""

    def action_leave(self) -> None:
        self.post_message(self.Leave())

    def action_fullscreen(self) -> None:
        self.post_message(self.ToggleFullscreen())

    def action_close(self) -> None:
        self.post_message(self.Close())

    def action_copy(self) -> None:
        self.screen.action_copy_text()
        self.screen.clear_selection()
        self.app.notify("Copied to the clipboard", timeout=2)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "copy":
            return self.text_selection is not None
        return True

    def check_consume_key(self, key: str, character: str | None) -> bool:
        # Every other key goes to the monitor, so the footer hides the tab's keys.
        return key not in OWN_KEYS

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.screen_: TermScreen = TermScreen(80, 24, self._reply)
        self.stream = pyte.ByteStream(self.screen_)
        self.process: MonitorProcess | None = None
        self.scroll = 0  # lines scrolled back into history; 0 follows output
        self._pending = False
        self._waiting: tuple | None = None  # start() before the widget had a size

    # -- process --

    def start(self, command: str, cwd: Path, env: dict[str, str]) -> None:
        """Run `command`; waits for the widget's first layout if it has no size yet,
        so the command never sees a tiny terminal."""
        self.stop()
        self.process = None
        if not self.content_size.width or not self.content_size.height:
            self._waiting = (command, cwd, env)
            self.refresh(layout=True)
            return
        self._waiting = None
        cols, lines = self._term_size()
        self.screen_ = TermScreen(cols, lines, self._reply)
        self.stream = pyte.ByteStream(self.screen_)
        self.scroll = 0
        proc: MonitorProcess | None = None

        # Callbacks from a monitor that was since replaced are dropped.
        def output(data: bytes) -> None:
            if self.process is proc:
                self._output(data)

        def exited(code: int) -> None:
            if self.process is proc:
                self._exited(code)

        proc = MonitorProcess(command, cwd, env, cols, lines, output, exited)
        self.process = proc
        self.refresh()

    def stop(self, then: Callable[[], None] | None = None) -> None:
        """End the command; `then` is called once it has exited."""
        self._waiting = None
        if self.process is not None:
            if then is not None:
                self.process.when_exited(then)
            self.process.close()
        elif then is not None:
            then()

    def devices(self) -> set[str]:
        """The /dev files the running command holds or names (see MonitorProcess)."""
        if self._waiting is not None:
            return command_devices(self._waiting[0])
        if self.process is not None and self.process.running:
            return self.process.devices()
        return set()

    @property
    def running(self) -> bool:
        return self._waiting is not None or (self.process is not None and self.process.running)

    def _reply(self, data: str) -> None:
        if self.process is not None:
            self.process.write(data.encode())

    def _output(self, data: bytes) -> None:
        before = len(self.screen_.scrollback)
        self.stream.feed(data)
        if self.scroll:  # keep the view still while reading history
            grown = len(self.screen_.scrollback) - before
            self.scroll = min(self.scroll + grown, len(self.screen_.scrollback))
        if not self._pending:  # redraw at most once per frame
            self._pending = True
            self.call_after_refresh(self._redraw)

    def _redraw(self) -> None:
        self._pending = False
        self.refresh()

    def _exited(self, returncode: int) -> None:
        self.write_note(f"[monitor exited with code {returncode}]")
        self.post_message(self.Exited(returncode))

    def write_note(self, text: str) -> None:
        """A line from lazyzephyr itself, shown in the terminal."""
        self.stream.feed(b"\r\n\x1b[2;7m " + text.encode() + b" \x1b[0m\r\n")
        self.refresh()

    def on_unmount(self) -> None:
        self.stop()

    # -- size --

    def _term_size(self) -> tuple[int, int]:
        size = self.content_size
        return max(size.width, 2), max(size.height, 1)

    def on_resize(self, event: events.Resize) -> None:
        if self._waiting is not None and event.size.width and event.size.height:
            self.start(*self._waiting)
            return
        cols, lines = self._term_size()
        if (cols, lines) != (self.screen_.columns, self.screen_.lines):
            self.screen_.resize(lines, cols)
            if self.process is not None:
                self.process.resize(cols, lines)
        self.refresh()

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        return container.height

    # -- drawing --

    def render_line(self, y: int) -> Strip:
        screen = self.screen_
        width = self.content_size.width
        row = y - self.scroll
        if row >= screen.lines:
            return Strip.blank(width)
        line = screen.line_at(row) or {}
        # Selections use absolute line numbers, so they stay put as output scrolls.
        absolute = screen.scrolled + row
        span = None
        if self.text_selection is not None:
            span = self.text_selection.get_span(absolute)
            if span is not None:
                span = (span[0], screen.columns if span[1] == -1 else span[1])
                # Only its background: its colour resolves to the same as the
                # background here, which would hide the text.
                selected = Style(
                    bgcolor=self.screen.get_component_rich_style("screen--selection").bgcolor,
                    reverse=False,
                )
        show_cursor = (
            self.has_focus
            and self.scroll == 0
            and row == screen.cursor.y
            and not screen.cursor.hidden
        )
        segments = []
        text, style = [], None
        for x in range(min(screen.columns, width)):
            ch = line.get(x) if line else None
            if ch is None:
                data, st = " ", Style()
            else:
                data = ch.data or " "
                st = char_style(
                    ch.fg, ch.bg, ch.bold, ch.italics, ch.underscore, ch.strikethrough, ch.reverse
                )
            if show_cursor and x == screen.cursor.x:
                st = st + Style(reverse=not st.reverse)
            if span is not None and span[0] <= x < span[1]:
                st = st + selected
            if st is not style and text:
                segments.append(Segment("".join(text), style))
                text = []
            style = st
            text.append(data)
        if text:
            segments.append(Segment("".join(text), style))
        strip = Strip(segments).adjust_cell_length(width).apply_style(self.rich_style)
        return strip.apply_offsets(0, absolute)

    # -- mouse selection (Textual drives it: drag selects, ctrl+c copies) --

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        screen = self.screen_
        first = screen.scrolled - len(screen.scrollback)
        last = screen.scrolled + screen.lines - 1
        start = max(selection.start.y if selection.start else first, first)
        end = min(selection.end.y if selection.end else last, last)
        lines = []
        for absolute in range(start, end + 1):
            span = selection.get_span(absolute)
            if span is None:
                continue
            text = screen.line_text(absolute - screen.scrolled)
            lines.append(text[span[0] :] if span[1] == -1 else text[span[0] : span[1]])
        return "\n".join(lines).rstrip("\n"), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        self.refresh_bindings()  # the footer's "Copy selection" comes and goes
        self.refresh()

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    # -- input --

    def _scroll_by(self, lines: int) -> None:
        self.scroll = max(0, min(self.scroll + lines, len(self.screen_.scrollback)))
        self.refresh()

    async def _on_key(self, event: events.Key) -> None:
        # Every key is ours: none reaches the tab's or the app's bindings.
        event.stop()
        event.prevent_default()
        own = OWN_KEYS.get(event.key)
        if own is not None:
            getattr(self, f"action_{own}")()
            return
        if event.key == "ctrl+c" and self.text_selection is not None:
            self.action_copy()
            return
        if event.key == "shift+pageup":
            self._scroll_by(max(self.content_size.height - 1, 1))
            return
        if event.key == "shift+pagedown":
            self._scroll_by(-max(self.content_size.height - 1, 1))
            return
        if self.process is None or not self.process.running:
            return
        data = key_bytes(event, DECCKM in self.screen_.mode)
        if data:
            if self.scroll:
                self.scroll = 0
                self.refresh()
            self.process.write(data)

    def on_paste(self, event: events.Paste) -> None:
        event.stop()
        if self.process is not None and self.process.running:
            self.process.write(event.text.encode())

    def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        event.stop()
        self._scroll_by(3)

    def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        event.stop()
        self._scroll_by(-3)
