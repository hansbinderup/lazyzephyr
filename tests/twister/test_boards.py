import json
from pathlib import Path

from lazyzephyr.twister import boards
from lazyzephyr.twister.jobs import Hardware


def test_save_and_load_round_trip(tmp_path: Path):
    setups = {
        "frdm_k64f": Hardware(
            enabled=True,
            serial="/dev/ttyACM0",
            baud=921600,
            runner="jlink",
            dev_id="683",
            fixtures=["gpio_loopback"],
        ),
        "native_sim": Hardware(fixtures=["f"]),
        "empty": Hardware(),
    }
    boards.save(tmp_path, setups)
    data = json.loads((tmp_path / "hardware.json").read_text())
    assert "empty" not in data  # nothing to remember
    assert data["native_sim"] == {"fixtures": ["f"], "enabled": False}
    loaded = boards.load_saved(tmp_path)
    assert loaded == {"frdm_k64f": setups["frdm_k64f"], "native_sim": setups["native_sim"]}


def test_load_skips_malformed_entries(tmp_path: Path):
    assert boards.load_saved(tmp_path) == {}  # no file yet
    (tmp_path / "hardware.json").write_text(
        json.dumps(
            {
                "good": {"enabled": True, "serial": "/dev/x"},
                "bad_baud": {"baud": 0},
                "bad_key": {"port": "/dev/x"},
                "not_a_table": 3,
            }
        )
    )
    assert boards.load_saved(tmp_path) == {"good": Hardware(enabled=True, serial="/dev/x")}
    (tmp_path / "hardware.json").write_text("{broken")
    assert boards.load_saved(tmp_path) == {}


def test_simulated_by_name():
    for name in ("native_sim", "native_sim/native/64", "qemu_x86", "nrf52_bsim", "unit_testing"):
        assert boards.simulated_by_name(name), name
    for name in ("frdm_k64f", "nrf52840dk/nrf52840", "qemu"):
        assert not boards.simulated_by_name(name), name


def test_board_type_from_twister_yaml(tmp_path: Path):
    (tmp_path / "myboard_soc.yaml").write_text("identifier: myboard/soc\ntype: mcu\n")
    (tmp_path / "myboard_soc_sim.yaml").write_text("identifier: myboard/soc/sim\ntype: sim\n")
    (tmp_path / "broken.yaml").write_text(": [")
    assert boards.board_type(tmp_path, "myboard/soc") == "mcu"
    assert boards.board_type(tmp_path, "myboard/soc/sim") == "sim"
    assert boards.board_type(tmp_path, "myboard") == "mcu"  # the first target
    assert boards.board_type(tmp_path, "other") is None
