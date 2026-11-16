import argparse
import re
from pathlib import Path

import pytest

from lazyzephyr.twister.cli import build_parser, config_from_args
from lazyzephyr.twister.config import ConfigError, find_config, load_config


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_load_valid_config_resolves_paths_relative_to_file(tmp_path):
    path = write(
        tmp_path / ".lazyzephyr.toml",
        """
roots = ["tests"]
platforms = ["native_sim/native/64"]
integration = false
watch = ["lib"]
jobs = 4
extra_args = ["--coverage"]
""",
    )
    cfg = load_config(path)
    assert cfg.roots == [tmp_path / "tests"]
    assert cfg.watch == [tmp_path / "lib"]
    assert cfg.platforms == ["native_sim/native/64"]
    assert cfg.integration is False and cfg.verbose is None
    assert cfg.jobs == 4 and cfg.extra_args == ["--coverage"]


@pytest.mark.parametrize(
    "body, message",
    [
        ("platform = ['x']", "unknown key(s): platform"),
        ("roots = 'tests'", "'roots' must be a list"),
        ("integration = 'yes'", "'integration' must be true or false"),
        ("jobs = 0", "'jobs' must be a positive integer"),
        ("jobs = true", "'jobs' must be a positive integer"),
        ("twister = ''", "'twister' must be a non-empty string"),
        ("roots = [", "invalid TOML"),
    ],
)
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
    write(
        tmp_path / ".lazyzephyr.toml",
        """
platforms = ["qemu_x86"]
integration = false
auto_run = true
twister = "my-twister --flag"
extra_args = ["--coverage"]
""",
    )
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


def test_hardware_tables_per_platform(tmp_path):
    cfg = load_config(
        write(
            tmp_path / ".lazyzephyr.toml",
            """
case_results = true

[hardware.frdm_k64f]
serial = "/dev/ttyACM0"
baud = 921600
runner = "jlink"
dev_id = "683"
fixtures = ["gpio_loopback"]

[hardware."nrf52840dk/nrf52840"]
fixtures = ["x"]
""",
        )
    )
    assert cfg.case_results is True
    assert set(cfg.boards) == {"frdm_k64f", "nrf52840dk/nrf52840"}
    hw = cfg.boards["frdm_k64f"]
    assert hw.enabled is True  # a serial port alone turns it on
    assert (hw.serial, hw.baud, hw.runner, hw.dev_id) == ("/dev/ttyACM0", 921600, "jlink", "683")
    assert hw.fixtures == ["gpio_loopback"]
    assert cfg.boards["nrf52840dk/nrf52840"].enabled is False


@pytest.mark.parametrize(
    "body, message",
    [
        ("hardware = 1", "'hardware' must be a table"),
        ("[hardware]\nserial = 'x'", "put board settings under [hardware.<platform>]"),
        ("[hardware.b]\nport = 'x'", "unknown key(s) in [hardware.b]: port"),
        ("[hardware.b]\nenabled = 1", "'hardware.b.enabled' must be true or false"),
        ("[hardware.b]\nserial = ''", "'hardware.b.serial' must be a non-empty string"),
        ("[hardware.b]\nbaud = 0", "'hardware.b.baud' must be a positive integer"),
        ("[hardware.b]\nfixtures = 'x'", "'hardware.b.fixtures' must be a list"),
        ('[hardware."b/soc"]\nbaud = 0', "'hardware.\"b/soc\".baud' must be a positive integer"),
        ("case_results = 'yes'", "'case_results' must be true or false"),
    ],
)
def test_invalid_hardware_is_rejected(tmp_path, body, message):
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(write(tmp_path / ".lazyzephyr.toml", body))


def test_cli_hardware_flags_override_config(tmp_path, monkeypatch):
    (tmp_path / "tests").mkdir()
    write(
        tmp_path / ".lazyzephyr.toml",
        """
roots = ["tests"]
platforms = ["frdm_k64f"]
[hardware.frdm_k64f]
serial = "/dev/ttyACM0"
runner = "jlink"
fixtures = ["a"]
""",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)

    from_file = config_from_args(parse(), [])
    assert from_file.options.hardware.serial == "/dev/ttyACM0"
    assert from_file.options.case_results is False
    assert from_file.fixed_boards == {"frdm_k64f"}

    cli = config_from_args(
        parse(
            "--device-serial",
            "/dev/ttyUSB1",
            "--west-runner",
            "pyocd",
            "-X",
            "b",
            "-X",
            "a",
            "--case-results",
        ),
        [],
    )
    hw = cli.options.hardware
    assert (hw.enabled, hw.serial, hw.runner) == (True, "/dev/ttyUSB1", "pyocd")
    assert hw.fixtures == ["a", "b"]
    assert cli.options.case_results is True

    # The flags set up one platform's board, so they need exactly one.
    with pytest.raises(SystemExit, match="exactly one platform"):
        config_from_args(parse("-p", "a", "-p", "b", "--device-serial", "/dev/x"), [])


def test_saved_board_setups_are_loaded(tmp_path, monkeypatch):
    from lazyzephyr.twister import boards
    from lazyzephyr.twister.jobs import Hardware

    (tmp_path / "tests").mkdir()
    write(
        tmp_path / ".lazyzephyr.toml",
        """
roots = ["tests"]
[hardware.frdm_k64f]
serial = "/dev/from-file"
""",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    state = tmp_path / "state"
    boards.save(
        state,
        {
            "frdm_k64f": Hardware(enabled=True, serial="/dev/saved"),
            "nucleo_f429zi": Hardware(enabled=True, serial="/dev/ttyACM3"),
        },
    )

    cfg = config_from_args(parse("--state-dir", str(state)), [])
    assert cfg.options.boards["nucleo_f429zi"].serial == "/dev/ttyACM3"
    assert cfg.options.boards["frdm_k64f"].serial == "/dev/from-file"  # the file wins
    assert cfg.fixed_boards == {"frdm_k64f"}
