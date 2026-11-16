import json
from pathlib import Path

from lazyzephyr.twister import session
from lazyzephyr.twister.cli import build_parser, config_from_args


def test_save_and_load(tmp_path: Path):
    assert session.load(tmp_path) == session.Session()  # nothing saved yet
    saved = session.Session(
        platforms=["frdm_k64f"],
        case_results=True,
        integration=False,
        auto_run=True,
        platform_only=True,
        board_choice="frdm_k64f",
    )
    session.save(tmp_path, saved)
    assert session.load(tmp_path) == saved
    session.save(tmp_path, session.Session(case_results=False))
    assert json.loads((tmp_path / "session.json").read_text()) == {"case_results": False}


def test_malformed_settings_are_left_unset(tmp_path: Path):
    (tmp_path / "session.json").write_text(
        json.dumps(
            {
                "platforms": "frdm_k64f",
                "case_results": "yes",
                "auto_run": True,
                "board_choice": "",
            }
        )
    )
    assert session.load(tmp_path) == session.Session(auto_run=True)
    (tmp_path / "session.json").write_text("[")
    assert session.load(tmp_path) == session.Session()


def test_saved_session_between_cli_and_config_file(tmp_path: Path, monkeypatch):
    (tmp_path / "tests").mkdir()
    (tmp_path / ".lazyzephyr.toml").write_text(
        'roots = ["tests"]\nplatforms = ["from_file"]\ncase_results = false\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    state = tmp_path / "state"

    def config(*argv):
        return config_from_args(build_parser().parse_args(["--state-dir", str(state), *argv]), [])

    cfg = config()
    assert cfg.options.platforms == ["from_file"] and cfg.options.case_results is False

    session.save(
        state,
        session.Session(
            platforms=["saved"], case_results=True, platform_only=True, board_choice="saved"
        ),
    )
    cfg = config()
    assert cfg.options.platforms == ["saved"] and cfg.options.case_results is True
    assert cfg.platform_only and cfg.board_choice == "saved"

    cfg = config("-p", "from_cli")
    assert cfg.options.platforms == ["from_cli"] and cfg.options.case_results is True

    session.save(state, session.Session(platforms=[], platform_only=True))
    cfg = config()
    assert cfg.options.platforms == []  # cleared in the app: stays cleared
    assert not cfg.platform_only  # nothing to filter on
