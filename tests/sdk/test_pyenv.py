"""The workspace's .venv: setup commands, its state, and the environment for builds."""

import os
import sys
from pathlib import Path

import pytest

from lazyzephyr.builder import runner
from lazyzephyr.dts.workspace import Workspace
from lazyzephyr.sdk import pyenv
from lazyzephyr.sdk.app import LazySdkApp, SdkView, SetupScreen
from lazyzephyr.sdk.model import Settings, sdk_env

from .conftest import FAKE_NIX, HERE
from .test_view import status, wait_for


def fake_venv(topdir: Path, version="3.12.4", west=True) -> Path:
    venv = pyenv.venv_path(topdir)
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {version}\n")
    if west:
        (venv / "bin" / "west").write_text("")
    return venv


def zephyr(tmp_path: Path, packages=True) -> Path:
    base = tmp_path / "zephyr"
    (base / "scripts" / "west_commands").mkdir(parents=True)
    (base / "scripts" / "requirements.txt").write_text("-r requirements-base.txt\n")
    (base / "scripts" / "requirements-base.txt").write_text("pyyaml\n")
    if packages:
        (base / "scripts" / "west_commands" / "packages.py").write_text("")
    return base


def test_setup_commands(tmp_path):
    base = zephyr(tmp_path)
    venv = pyenv.venv_path(tmp_path)
    create, west, packages = pyenv.setup_commands(tmp_path, base, "/usr/bin/python3")
    assert create == ["/usr/bin/python3", "-m", "venv", str(venv)]
    assert west == [
        str(venv / "bin" / "python"),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
        "west",
    ]
    assert packages == [str(venv / "bin" / "west"), "packages", "pip", "--install"]
    fake_venv(tmp_path)  # an existing venv is reused
    assert pyenv.setup_commands(tmp_path, base)[0][1:3] == ["-m", "pip"]
    old = zephyr(tmp_path / "old", packages=False)  # before `west packages`
    assert pyenv.setup_commands(tmp_path, old)[-1][-2:] == [
        "-r",
        str(old / "scripts" / "requirements.txt"),
    ]


def test_state_follows_the_requirements(tmp_path):
    base = zephyr(tmp_path)
    assert not pyenv.inspect(tmp_path, base).exists
    fake_venv(tmp_path)
    env = pyenv.inspect(tmp_path, base)
    assert env.exists and env.has_west and env.python == (3, 12) and not env.ours
    pyenv.mark_set_up(tmp_path, base)
    assert pyenv.inspect(tmp_path, base).ready
    (base / "scripts" / "requirements-base.txt").write_text("pyyaml>=6\n")
    env = pyenv.inspect(tmp_path, base)
    assert env.ours and not env.up_to_date and not env.ready


def test_old_python_is_not_ok(tmp_path):
    fake_venv(tmp_path, version="3.11.9")
    assert not pyenv.inspect(tmp_path, zephyr(tmp_path)).python_ok


def test_builds_run_from_the_workspace_venv(tmp_path, monkeypatch):
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    store = Path(os.environ["LAZYZEPHYR_SDK_DIR"])
    assert sdk_env({"PATH": "/usr/bin"}, None, store, topdir=tmp_path) == {"PATH": "/usr/bin"}
    venv = fake_venv(tmp_path)
    env = sdk_env({"PATH": "/usr/bin"}, None, store, topdir=tmp_path)
    assert env["PATH"] == f"{venv / 'bin'}:/usr/bin" and env["VIRTUAL_ENV"] == str(venv)
    assert runner.west_command(tmp_path) == [str(venv / "bin" / "west")]
    mine = {"PATH": "/usr/bin", "VIRTUAL_ENV": "/home/me/venv"}
    assert sdk_env(mine, None, store, topdir=tmp_path) == mine  # an active venv wins
    Settings(use=False).save(store)
    assert "VIRTUAL_ENV" not in sdk_env({}, None, store, topdir=tmp_path)


@pytest.fixture
def no_west(monkeypatch):
    """No west anywhere but in the workspace's .venv."""
    monkeypatch.setattr(runner.shutil, "which", lambda name: None)
    monkeypatch.setitem(sys.modules, "west", None)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)


def fake_python(tmp_path: Path) -> str:
    wrapper = tmp_path / "python3"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_python.py"}" "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


async def test_set_up_python_then_install_with_its_west(setup, tmp_path, no_west):
    zephyr_base, _, _, calls = setup
    (zephyr_base / "scripts" / "west_commands" / "packages.py").write_text("")
    (zephyr_base / "scripts" / "requirements.txt").write_text("west\n")
    topdir = zephyr_base.parent
    view = SdkView(
        Workspace(topdir, zephyr_base), nix=FAKE_NIX, python=fake_python(tmp_path), id="v"
    )
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await wait_for(lambda: view.available is not None)
        assert "no .venv in the workspace" in status(view)
        assert not view.availability["west"].ok
        assert "p sets up" in view.availability["west"].reason

        await pilot.press("p")
        await wait_for(lambda: view.pyenv is not None and view.pyenv.ready)
        assert [c[:4] for c in calls()] == [
            ["python", "-m", "venv", str(topdir / ".venv")],
            ["python", "-m", "pip", "install"],
            ["west", "packages", "pip", "--install"],
        ]
        assert ".venv · Python 3.12" in status(view)
        assert view.availability["west"].ok

        # west sdk (the default) now runs from the venv.
        view.toolchain_list.focus()
        view.toolchain_list.highlighted = view._rows.index("arm-zephyr-eabi")
        await pilot.press("space", "i")
        await wait_for(lambda: view.installed is not None)
        assert calls()[-1][:3] == ["west", "sdk", "install"]
        assert view.installed.toolchains == ["arm-zephyr-eabi"]

        # New requirements in Zephyr: the tab says so.
        (zephyr_base / "scripts" / "requirements-base.txt").write_text("pyyaml\n")
        await pilot.press("r")
        await pilot.pause()
        assert "requirements changed" in status(view)


async def test_an_existing_venv_is_reused_after_asking(setup, tmp_path, no_west):
    zephyr_base, _, _, calls = setup
    topdir = zephyr_base.parent
    fake_venv(topdir, west=False)
    python = topdir / ".venv" / "bin" / "python"
    python.write_text(
        f'#!/bin/sh\nFAKE_VENV="{topdir / ".venv"}" exec "{sys.executable}" '
        f'"{HERE / "fake_python.py"}" "$@"\n'
    )
    python.chmod(0o755)
    view = SdkView(Workspace(topdir, zephyr_base), python=fake_python(tmp_path), id="v")
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await pilot.pause()
        assert "not set up by lazyzephyr" in status(view)
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("n")  # no: nothing runs
        await pilot.pause()
        assert calls() == []
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("y")
        await wait_for(lambda: view.pyenv.ready)
        assert calls()[0][:3] == ["python", "-m", "pip"]  # no new venv


async def test_setup_steps_show_what_is_missing(setup, tmp_path, no_west):
    zephyr, store, _, calls = setup
    (zephyr / "scripts" / "requirements.txt").write_text("west\n")
    Settings().save(store)  # the setup steps are on by default
    view = SdkView(
        Workspace(zephyr.parent, zephyr), nix=FAKE_NIX, python=fake_python(tmp_path), id="v"
    )
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await wait_for(lambda: isinstance(pilot.app.screen, SetupScreen))
        assert [done for done, _, _ in pilot.app.screen.steps] == [False, False]
        assert pilot.app.screen.next_step == "set up the Python environment"
        await pilot.press("enter")  # sets up the .venv
        await wait_for(lambda: view.pyenv is not None and view.pyenv.ready)
        assert calls()[0][1:3] == ["-m", "venv"]
        # Then the overlay comes back for the SDK, which focuses the toolchains.
        await wait_for(lambda: isinstance(pilot.app.screen, SetupScreen))
        assert [done for done, _, _ in pilot.app.screen.steps] == [True, False]
        await pilot.press("enter")
        await pilot.pause()
        assert pilot.app.focused is view.toolchain_list


async def test_setup_steps_can_be_turned_off(setup):
    _, store, make, _ = setup
    Settings().save(store)
    view = make()
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await wait_for(lambda: isinstance(pilot.app.screen, SetupScreen))
        await pilot.press("n")
        await pilot.pause()
        assert not isinstance(pilot.app.screen, SetupScreen)
        assert not Settings.load(store).guide
        await pilot.press("g")  # still there on demand
        await pilot.pause()
        assert isinstance(pilot.app.screen, SetupScreen)
        await pilot.press("escape")


def own_west(tmp_path, monkeypatch) -> Path:
    """west on the PATH from a setup of the user's own, with a python3 beside it."""
    bin_dir = tmp_path / "own-bin"
    bin_dir.mkdir()
    for name, body in (
        ("west", "#!/bin/sh\n"),
        ("python3", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n'),
    ):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir / "west"


async def test_west_of_your_own_needs_no_venv(setup, tmp_path, monkeypatch):
    """A Nix shell (or any setup) with west and Zephyr's packages: nothing to set up."""
    zephyr, store, make, _ = setup
    (zephyr / "scripts" / "requirements-base.txt").write_text(
        "# Zephyr's base packages\npytest>=7\nwindows-curses; sys_platform == 'win32'\n"
    )
    own_west(tmp_path, monkeypatch)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    Settings().save(store)  # the setup steps on
    view = make()
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await wait_for(lambda: view.available is not None and view.own_missing is not None)
        assert view.own_missing == [] and view.python_ready()
        assert "with Zephyr's packages" in status(view)
        # The steps still come up for the SDK, but go straight to the toolchains.
        await wait_for(lambda: isinstance(pilot.app.screen, SetupScreen))
        assert [done for done, _, _ in pilot.app.screen.steps] == [True, False]
        assert pilot.app.screen.next_step == "choose the toolchains"


async def test_west_of_your_own_without_zephyrs_packages(setup, tmp_path, monkeypatch):
    """west alone isn't enough: missing packages are named, and p still sets them up."""
    zephyr, store, make, _ = setup
    (zephyr / "scripts" / "requirements-base.txt").write_text("pytest\nno-such-package-here\n")
    own_west(tmp_path, monkeypatch)
    Settings().save(store)  # the setup steps on
    view = make()
    async with LazySdkApp(view).run_test(size=(160, 45)) as pilot:
        await wait_for(lambda: view.own_missing is not None)
        assert view.own_missing == ["no-such-package-here"] and not view.python_ready()
        assert "lacks no-such-package-here" in status(view)
        assert "p sets up a workspace .venv" in status(view)
        # The steps waited for the check, so they ask for the .venv after all.
        await wait_for(lambda: isinstance(pilot.app.screen, SetupScreen))
        assert pilot.app.screen.next_step == "set up the Python environment"


def test_base_requirements_and_the_python_beside_west(tmp_path):
    base = tmp_path / "zephyr"
    (base / "scripts").mkdir(parents=True)
    (base / "scripts" / "requirements-base.txt").write_text(
        "# comment\n-r other.txt\npyelftools>=0.29\nPyYAML>=6.0  # yaml\n"
        "windows-curses; sys_platform == 'win32'\n\nwest\n"
    )
    assert pyenv.base_requirements(base) == ["pyelftools", "PyYAML", "west"]
    assert pyenv.base_requirements(tmp_path) is None
    west = tmp_path / "west"
    west.write_text("#!/usr/bin/env python3.12\n")
    assert pyenv.python_beside(str(west)) == "python3.12"
    west.write_text("#!/bin/sh\n")
    assert pyenv.python_beside(str(west)) is None
