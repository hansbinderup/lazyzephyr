"""Drive the SDK tab headlessly, with fake west and nix."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from lazyzephyr.dts.workspace import Workspace
from lazyzephyr.sdk import backends
from lazyzephyr.sdk.app import LazySdkApp, SdkView, cache_dir
from lazyzephyr.sdk.model import Settings, host_platform, hosttools_path, sdk_env, sdk_path

HERE = Path(__file__).parent
FAKE_WEST = [sys.executable, str(HERE / "fake_west.py")]
FAKE_NIX = [sys.executable, str(HERE / "fake_nix.py")]


async def wait_for(predicate, timeout=15.0):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


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
    monkeypatch.setenv("FAKE_NIX_STORE", str(tmp_path / "nix-store"))
    (tmp_path / "nix-store").mkdir()
    store = Path(os.environ["LAZYZEPHYR_SDK_DIR"])

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def view():
        return SdkView(
            Workspace(tmp_path / "ws", zephyr), west=FAKE_WEST, nix=FAKE_NIX, id="sdk-view"
        )

    return zephyr, store, view, calls


async def choose(pilot, view, backend):
    view.backend_list.focus()
    view.backend_list.highlighted = ["nix", "west"].index(backend)
    await pilot.press("enter")
    view.toolchain_list.focus()
    await pilot.pause()


async def tick(pilot, view, *names):
    for name in names:
        view.toolchain_list.highlighted = view._rows.index(name)
        await pilot.press("space")


def status(view) -> str:
    return str(view.query_one("#s-status").render())


async def test_west_install_add_and_remove(setup):
    zephyr, store, make, calls = setup
    view = make()
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await wait_for(lambda: view.available is not None)
        assert "Zephyr SDK 1.0.1" in status(view) and "not installed" in status(view)
        assert view._rows == [
            "hosttools",
            "arm-zephyr-eabi",
            "riscv64-zephyr-elf",
            "x86_64-zephyr-elf",
        ]
        await pilot.press("i")  # no installer chosen yet: nothing runs
        await pilot.pause()
        assert calls() == []

        await choose(pilot, view, "west")
        assert Settings.load(store).backend == "west"
        await tick(pilot, view, "arm-zephyr-eabi")
        await pilot.press("i")
        await wait_for(lambda: view.installed is not None)
        assert calls()[-1] == [
            "west",
            *backends.west_install_args(
                "1.0.1", store, ["arm-zephyr-eabi"], True, "--gnu-toolchains"
            ),
        ]
        assert view.installed.toolchains == ["arm-zephyr-eabi"] and view.installed.hosttools
        assert "installed with west" in status(view)
        assert Settings.load(store).toolchains == ["arm-zephyr-eabi"]
        assert sdk_env({}, zephyr, store)["ZEPHYR_SDK_INSTALL_DIR"] == str(sdk_path(store, "1.0.1"))

        # Swap arm for riscv: arm is deleted (after asking), riscv added.
        await tick(pilot, view, "arm-zephyr-eabi", "riscv64-zephyr-elf")
        assert view.changes() == (["riscv64-zephyr-elf"], ["arm-zephyr-eabi"])
        await pilot.press("i")
        await pilot.pause()
        await pilot.press("y")
        await wait_for(lambda: view.installed.toolchains == ["riscv64-zephyr-elf"])
        assert calls()[-1][-3:] == ["--gnu-toolchains", "riscv64-zephyr-elf", "--no-hosttools"]


async def test_nix_install_and_switch_to_west(setup):
    _, store, make, calls = setup
    view = make()
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await wait_for(lambda: view.available is not None)
        await choose(pilot, view, "nix")
        await wait_for(lambda: view.nix_versions is not None)
        assert view.nix_attr() == "sdk-1_0"
        await tick(pilot, view, "x86_64-zephyr-elf")
        await pilot.press("i")
        await wait_for(lambda: view.installed is not None)
        builds = [c for c in calls() if "build" in c]
        assert len(builds) == 2  # the SDK, then its host tools
        assert sdk_path(store, "1.0.1").is_symlink() and hosttools_path(store, "1.0.1").is_symlink()
        assert view.installed.backend == "nix"
        assert view.installed.toolchains == ["x86_64-zephyr-elf"]
        assert "installed with nix" in status(view)

        # Switching to west replaces the Nix link after asking, keeping new ticks.
        await choose(pilot, view, "west")
        await tick(pilot, view, "arm-zephyr-eabi")
        await pilot.press("i")
        await pilot.pause()
        await pilot.press("y")
        await wait_for(lambda: view.installed is not None and view.installed.backend == "west")
        assert calls()[-1][:2] == ["west", "sdk"]
        assert view.installed.toolchains == ["arm-zephyr-eabi", "x86_64-zephyr-elf"]
        assert not sdk_path(store, "1.0.1").is_symlink()


async def test_nix_without_this_version_says_so(setup):
    zephyr, store, make, calls = setup
    (zephyr / "SDK_VERSION").write_text("1.0.2\n")
    cache = backends.release_cache(cache_dir(), "1.0.1")
    cache.rename(backends.release_cache(cache_dir(), "1.0.2"))
    Settings(backend="nix", zephyr_nix="github:me/zephyr-nix/main").save(store)
    view = make()
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await wait_for(lambda: view.nix_versions is not None and view.available is not None)
        assert "zephyr-nix has no SDK 1.0.2 (it has 0.16.9, 0.17.4, 1.0.1)" in status(view)
        assert '"github:me/zephyr-nix/main"' in calls()[0][-1]  # the settings' zephyr-nix
        await tick(pilot, view, "arm-zephyr-eabi")
        await pilot.press("i")
        await pilot.pause()
        assert not [c for c in calls() if "build" in c]


async def test_remove_sdk_and_use_switch(setup):
    zephyr, store, make, _ = setup
    view = make()
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await wait_for(lambda: view.available is not None)
        await choose(pilot, view, "west")
        await tick(pilot, view, "arm-zephyr-eabi")
        await pilot.press("i")
        await wait_for(lambda: view.installed is not None)
        await pilot.press("u")
        assert not Settings.load(store).use and "for builds: off" in status(view)
        assert "ZEPHYR_SDK_INSTALL_DIR" not in sdk_env({}, zephyr, store)
        await pilot.press("u")
        await pilot.press("D")
        await pilot.pause()
        await pilot.press("y")
        await wait_for(lambda: view.installed is None)
        assert not sdk_path(store, "1.0.1").exists()


async def test_without_a_zephyr_tree(tmp_path):
    view = SdkView(None, west=FAKE_WEST, nix=FAKE_NIX, id="sdk-view")
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        assert "not found" in status(view)
        await pilot.press("i")
        await pilot.pause()


async def test_host_tools_are_a_change_of_their_own(setup):
    _, _, make, calls = setup
    view = make()
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await wait_for(lambda: view.available is not None)
        await choose(pilot, view, "west")
        await tick(pilot, view, "hosttools", "arm-zephyr-eabi")  # host tools off
        await pilot.press("i")
        await wait_for(lambda: view.installed is not None)
        assert calls()[-1][-1] == "--no-hosttools" and not view.installed.hosttools
        await tick(pilot, view, "hosttools")
        assert view.changes() == (["hosttools"], [])
        await pilot.press("i")
        await wait_for(lambda: view.installed.hosttools)
        assert calls()[-1][-1] == "--no-gnu-toolchains"  # only the host tools
        await tick(pilot, view, "hosttools")
        assert view.changes() == ([], ["hosttools"])
        await pilot.press("i")
        await pilot.pause()
        await pilot.press("y")
        await wait_for(lambda: not view.installed.hosttools)
        assert view.installed.toolchains == ["arm-zephyr-eabi"]
