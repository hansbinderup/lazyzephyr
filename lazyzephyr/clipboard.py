"""Copy to the system clipboard, not only through the terminal.

Textual copies with the OSC 52 escape sequence, which many terminals ignore
(VTE ones such as GNOME Terminal) and tmux drops unless `set-clipboard on`.
So the text also goes to the desktop's clipboard tool when there is one; OSC 52
stays for SSH sessions, where only the terminal can reach the local clipboard.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys


def clipboard_command() -> list[str] | None:
    """The command that puts its stdin on the system clipboard, or None."""
    candidates = []
    if os.environ.get("WAYLAND_DISPLAY"):
        candidates.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        candidates += [["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]]
    if sys.platform == "darwin":
        candidates.append(["pbcopy"])
    for command in candidates:
        if shutil.which(command[0]):
            return command
    return None


def copy_to_system_clipboard(text: str) -> bool:
    """Put `text` on the system clipboard; False if there is no tool or it failed."""
    command = clipboard_command()
    if command is None:
        return False
    try:
        # The tools fork to keep serving the clipboard; the call itself returns at once.
        subprocess.run(
            command,
            input=text.encode(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return True


class SystemClipboard:
    """App mixin: every copy (selections, y in dialogs...) also reaches the system clipboard."""

    def copy_to_clipboard(self, text: str) -> None:
        super().copy_to_clipboard(text)  # OSC 52
        copy_to_system_clipboard(text)
