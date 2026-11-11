import argparse
from pathlib import Path

import pytest

from lazyzephyr.twister.cli import build_parser, config_from_args
from lazyzephyr.twister.config import ConfigError, find_config, load_config


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_load_valid_config_resolves_paths_relative_to_file(tmp_path):
    path = write(tmp_path / ".lazyzephyr.toml", """
roots = ["tests"]
platforms = ["native_sim/native/64"]
integration = false
watch = ["lib"]
jobs = 4
extra_args = ["--coverage"]
""")
    cfg = load_config(path)
    assert cfg.roots == [tmp_path / "tests"]
    assert cfg.watch == [tmp_path / "lib"]
    assert cfg.platforms == ["native_sim/native/64"]
    assert cfg.integration is False and cfg.verbose is None
    assert cfg.jobs == 4 and cfg.extra_args == ["--coverage"]


@pytest.mark.parametrize("body, message", [
    ("platform = ['x']", "unknown key(s): platform"),
    ("roots = 'tests'", "'roots' must be a list"),
    ("integration = 'yes'", "'integration' must be true or false"),
    ("jobs = 0", "'jobs' must be a positive integer"),
    ("jobs = true", "'jobs' must be a positive integer"),
    ("twister = ''", "'twister' must be a non-empty string"),
    ("roots = [", "invalid TOML"),
])
def test_invalid_config_is_rejected_with_a_clear_message(tmp_path, body, message):
    path = write(tmp_path / ".lazyzephyr.toml", body)
    with pytest.raises(ConfigError, match=message.replace("(", r"\(").replace(")", r"\)")):
        load_config(path)


def test_find_config_stops_at_workspace(tmp_path):
    workspace = tmp_path / "ws"
    nested = workspace / "app" / "tests"
    nested.mkdir(parents=True)
    write(tmp_path / ".lazyzephyr.toml", "")  # outside the workspace: ignored
    assert find_config(nested, workspace) is None
    found = write(workspace / ".lazyzephyr.toml", "")
    assert find_config(nested, workspace) == found


def parse(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(list(argv))


def test_cli_overrides_config(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / "other").mkdir()
    write(tmp_path / ".lazyzephyr.toml", """
platforms = ["qemu_x86"]
integration = false
auto_run = true
twister = "my-twister --flag"
extra_args = ["--coverage"]
""")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)

    from_file = config_from_args(parse(), ["-v"])
    assert from_file.options.platforms == ["qemu_x86"]
    assert from_file.options.integration is False
    assert from_file.auto_run is True
    assert from_file.twister_cmd == ["my-twister", "--flag"]
    assert from_file.options.extra_args == ["--coverage", "-v"]
    assert from_file.config_file == tmp_path / ".lazyzephyr.toml"

    overridden = config_from_args(parse("-p", "native_sim", "-T", "other"), [])
    assert overridden.options.platforms == ["native_sim"]
    assert overridden.roots == [tmp_path / "other"]

    ignored = config_from_args(parse("--no-config"), [])
    assert ignored.options.platforms == [] and ignored.options.integration is True
    assert ignored.config_file is None


def test_broken_config_exits_with_message(tmp_path, monkeypatch):
    write(tmp_path / ".lazyzephyr.toml", "bogus = 1")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="unknown key"):
        config_from_args(parse(), [])
