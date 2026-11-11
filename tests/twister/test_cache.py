from pathlib import Path

from lazyzephyr.twister import cache
from lazyzephyr.twister.cli import build_parser, run


def make_state(state: Path) -> None:
    for name in ("run", "run.1", "run.12", "run-0", "run-0.1"):
        (state / name / "build").mkdir(parents=True)
        (state / name / "build" / "zephyr.exe").write_bytes(b"x" * 1000)
    (state / "results.json").write_text("{}")
    (state / "notes.txt").write_text("not ours")
    (state / "runner").mkdir()  # name merely starts with "run": not ours


def test_clear_build_output_keeps_results_and_unknown_files(tmp_path):
    state = tmp_path / "ws"
    make_state(state)
    builds, results = cache.usage(state)
    assert builds == 5000 and results == 2
    assert cache.clear(state) == 5000
    assert sorted(p.name for p in state.iterdir()) == ["notes.txt", "results.json", "runner"]


def test_clear_everything_only_removes_our_files(tmp_path):
    state = tmp_path / "ws"
    make_state(state)
    cache.clear(state, include_results=True)
    assert sorted(p.name for p in state.iterdir()) == ["notes.txt", "runner"]


def test_clear_does_not_follow_symlinks(tmp_path):
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep")
    state = tmp_path / "ws"
    state.mkdir()
    (state / "run").symlink_to(precious, target_is_directory=True)
    cache.clear(state, include_results=True)
    assert (precious / "keep.txt").read_text() == "keep"


def test_cli_cache_info_and_clear(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    here = cache.state_dir_for(tmp_path)
    other = cache.cache_root() / "other_workspace"
    make_state(here)
    make_state(other)

    run(build_parser().parse_args(["--cache-info"]), [])
    out = capsys.readouterr().out
    assert "* " in out and str(other) in out and "total" in out

    run(build_parser().parse_args(["--clear-cache"]), [])
    assert "freed 4.9 KB" in capsys.readouterr().out
    assert not cache.build_dirs(here) and cache.build_dirs(other)

    run(build_parser().parse_args(["--clear-cache", "all"]), [])
    assert not cache.build_dirs(other) and not cache.results_files(other)


def test_human():
    assert cache.human(512) == "512 B"
    assert cache.human(1536) == "1.5 KB"
    assert cache.human(3 * 1024**3) == "3.0 GB"
