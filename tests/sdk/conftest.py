"""Shared by the SDK tab tests: a Zephyr that needs SDK 1.0.1, and fake installers."""

import json
import os
import sys
from pathlib import Path

import pytest

from lazyzephyr.dts.workspace import Workspace
from lazyzephyr.sdk import backends
from lazyzephyr.sdk.app import SdkView, cache_dir
from lazyzephyr.sdk.model import Settings, host_platform

HERE = Path(__file__).parent
FAKE_WEST = [sys.executable, str(HERE / "fake_west.py")]
FAKE_NIX = [sys.executable, str(HERE / "fake_nix.py")]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A Zephyr that needs SDK 1.0.1, its toolchain list cached, fake installers."""
    zephyr = tmp_path / "ws" / "zephyr"
    (zephyr / "scripts" / "west_commands").mkdir(parents=True)
    (zephyr / "SDK_VERSION").write_text("1.0.1\n")
    (zephyr / "scripts" / "west_commands" / "sdk.py").write_text('"--gnu-toolchains"')
    system, arch = host_platform()
    names = [
        f"toolchain_gnu_{system}-{arch}_{tc}.tar.xz"
        for tc in ("arm-zephyr-eabi", "riscv64-zephyr-elf", "x86_64-zephyr-elf")
    ]
    cache = backends.release_cache(cache_dir(), "1.0.1")
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps(names))
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.delenv("ZEPHYR_TOOLCHAIN_VARIANT", raising=False)
    monkeypatch.setenv("FAKE_NIX_STORE", str(tmp_path / "nix-store"))
    (tmp_path / "nix-store").mkdir()
    store = Path(os.environ["LAZYZEPHYR_SDK_DIR"])
    Settings(guide=False).save(store)  # the setup steps have tests of their own

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def view():
        return SdkView(
            Workspace(tmp_path / "ws", zephyr), west=FAKE_WEST, nix=FAKE_NIX, id="sdk-view"
        )

    return zephyr, store, view, calls
