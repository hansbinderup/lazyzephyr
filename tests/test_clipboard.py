"""Copies reach the system clipboard tool, not only the terminal (OSC 52)."""

import os
import stat

from lazyzephyr.clipboard import SystemClipboard, clipboard_command, copy_to_system_clipboard


def fake_tool(bin_dir, name, out):
    tool = bin_dir / name
    tool.write_text(f"#!/bin/sh\ncat > {out}\n")
    tool.chmod(tool.stat().st_mode | stat.S_IEXEC)


def test_picks_the_tool_for_the_session(tmp_path, monkeypatch):
    fake_tool(tmp_path, "wl-copy", os.devnull)
    fake_tool(tmp_path, "xclip", os.devnull)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setenv("DISPLAY", ":0")
    assert clipboard_command() == ["wl-copy"]
    monkeypatch.delenv("WAYLAND_DISPLAY")
    assert clipboard_command() == ["xclip", "-selection", "clipboard"]
    monkeypatch.delenv("DISPLAY")  # e.g. over SSH: OSC 52 only
    assert clipboard_command() is None
    assert copy_to_system_clipboard("x") is False


def test_app_copy_reaches_the_tool(tmp_path, monkeypatch):
    out = tmp_path / "clipboard"
    fake_tool(tmp_path, "wl-copy", out)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    osc52 = []

    class Base:
        def copy_to_clipboard(self, text):
            osc52.append(text)

    class App(SystemClipboard, Base):
        pass

    App().copy_to_clipboard("uart:~$ kernel version\nZephyr version 4.2.0")
    assert osc52 == ["uart:~$ kernel version\nZephyr version 4.2.0"]
    assert out.read_text() == "uart:~$ kernel version\nZephyr version 4.2.0"
