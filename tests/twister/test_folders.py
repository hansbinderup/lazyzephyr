import argparse
from pathlib import Path

import pytest

from lazyzephyr.twister import folders
from lazyzephyr.twister.app import FolderScreen
from lazyzephyr.twister.cli import build_parser, config_from_args
from .test_app import make_app, wait_for


def test_count_tests_includes_subfolders_and_skips_output(test_tree):
    counts = folders.count_tests(test_tree)
    assert counts[test_tree] == 3  # alpha, beta, broken; twister-out is skipped
    assert counts[test_tree / "lib"] == 1
    assert counts[test_tree / "lib" / "alpha" / "src"] == 0
    assert test_tree / "twister-out" not in counts


def test_list_dirs_hides_hidden_and_twister_output(test_tree):
    (test_tree / ".cache").mkdir()
    assert [p.name for p in folders.list_dirs(test_tree)] == ["broken", "drivers", "lib"]


def test_normalize_drops_nested_and_duplicate_folders(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    assert folders.normalize([a / "x", a, b, a]) == [a, b]


def test_saved_selection_is_per_directory(tmp_path):
    state, one, two = tmp_path / "state", tmp_path / "one", tmp_path / "two"
    (one / "tests").mkdir(parents=True)
    two.mkdir()
    assert folders.load_saved(state, one) is None
    folders.save(state, one, [one / "tests"])
    assert folders.load_saved(state, one) == [one / "tests"]
    assert folders.load_saved(state, two) is None
    (one / "tests").rmdir()
    assert folders.load_saved(state, one) is None  # nothing left: ask again


def parse(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    work = tmp_path / "work"
    (work / "tests").mkdir(parents=True)
    (work / "app").mkdir()
    monkeypatch.chdir(work)
    return work


def test_first_start_asks_then_uses_saved_selection(workdir):
    first = config_from_args(parse(), [])
    assert first.pick_folders and first.roots_source == "default"
    assert first.roots == [workdir / "tests"]  # preselected in the browser

    folders.save(first.state_dir, workdir, [workdir / "app"])
    again = config_from_args(parse(), [])
    assert not again.pick_folders and again.roots == [workdir / "app"]
    assert again.roots_source == "saved"
    assert config_from_args(parse("--pick-folders"), []).pick_folders

    cli = config_from_args(parse("-T", "tests"), [])
    assert cli.roots == [workdir / "tests"] and cli.roots_source == "cli"

    (workdir / ".lazyzephyr.toml").write_text('roots = ["tests"]\n')
    from_file = config_from_args(parse(), [])
    assert from_file.roots == [workdir / "tests"] and from_file.roots_source == "config"


async def test_folder_browser_on_first_start(test_tree, tmp_path, fake_twister):
    app = make_app(test_tree, tmp_path, fake_twister)
    app.config.roots, app.config.pick_folders = [], True
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: isinstance(app.screen, FolderScreen))
        screen = app.screen
        await wait_for(lambda: test_tree in screen.counts)
        assert "3 test files" in str(screen.nodes[test_tree].label)

        await pilot.press("enter")  # nothing selected: refused
        assert app.screen is screen

        screen.nodes[test_tree].expand()
        await pilot.pause()
        screen.tree.move_cursor(screen.nodes[test_tree / "lib"])
        await pilot.press("space")
        await pilot.press("enter")
        await wait_for(lambda: "s:lib.alpha.default" in app.node_index)
        assert "s:drivers.beta" not in app.node_index
        assert folders.load_saved(tmp_path / "state", tmp_path) == [test_tree / "lib"]

        # And from inside the app: O opens it again with the current selection.
        await pilot.press("O")
        await wait_for(lambda: isinstance(app.screen, FolderScreen))
        screen = app.screen
        assert screen.selected == {test_tree / "lib"}
        assert str(screen.nodes[test_tree].label).startswith("[-]")
        screen.tree.move_cursor(screen.nodes[test_tree])
        await pilot.press("space")  # selecting the parent replaces the child
        assert screen.selected == {test_tree}
        await pilot.press("enter")
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        assert app.config.roots == [test_tree]


async def test_cancelled_first_start_falls_back_to_defaults(test_tree, tmp_path, fake_twister):
    app = make_app(test_tree, tmp_path, fake_twister)
    app.config.pick_folders = True
    async with app.run_test(size=(160, 50)) as pilot:
        await wait_for(lambda: isinstance(app.screen, FolderScreen))
        await pilot.press("escape")
        await wait_for(lambda: "s:drivers.beta" in app.node_index)
        assert folders.load_saved(tmp_path / "state", tmp_path) is None
