from lazyzephyr.dts.session import Session
from lazyzephyr.dts.workspace import find_board, find_workspace, inputs_for_board


def make(workspace, tmp_path):
    ws = find_workspace(workspace)
    inputs = inputs_for_board(ws, find_board(ws, "native_sim/native"), workspace / "app")
    s = Session(ws, inputs, tmp_path / "work")
    assert s.reload(), s.error
    return s


def test_bad_value_is_rolled_back(workspace, tmp_path):
    s = make(workspace, tmp_path)
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    before = overlay.read_text()
    i2c = next(n.path for n in s.model.edt.nodes if n.path.startswith("/i2c@"))
    r = s.set_property(i2c, "status", "bogus", overlay)
    assert not r.ok and overlay.read_text() == before
    r = s.set_property(i2c, "clock-frequency", "<&nope>", overlay)
    assert not r.ok and overlay.read_text() == before


def test_only_binding_properties(workspace, tmp_path):
    s = make(workspace, tmp_path)
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    r = s.set_property("/leds/led_0", "not-in-binding", "1", overlay)
    assert not r.ok


def test_upstream_files_not_writable(workspace, tmp_path):
    s = make(workspace, tmp_path)
    assert s.model.writable_files() == [workspace / "app" / "boards" / "native_sim.overlay"]


def test_remove_restores_earlier_value(workspace, tmp_path):
    s = make(workspace, tmp_path)
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    i2c = next(n.path for n in s.model.edt.nodes if n.path.startswith("/i2c@"))
    assert s.node(i2c).status == "okay"
    r = s.set_property(i2c, "status", None, overlay, delete=True)
    assert r.ok, r.message
    assert "status" not in overlay.read_text()


def test_chosen_and_aliases(workspace, tmp_path):
    s = make(workspace, tmp_path)
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    i2c = next(n for n in s.model.edt.nodes if n.path.startswith("/i2c@"))
    label = i2c.labels[0]
    # Change an alias, add a new one, and a chosen entry.
    r = s.set_property("/aliases", "led0", f"&{label}", overlay)
    assert r.ok, r.message
    assert s.model.edt.get_node("/aliases")._node.props["led0"].to_path().path == i2c.path
    r = s.set_property("/aliases", "my-bus", label, overlay)
    assert r.ok, r.message
    assert f"my-bus = &{label};" in overlay.read_text()
    r = s.set_property("/chosen", "zephyr,my-bus", f"&{label}", overlay)
    assert r.ok, r.message
    assert s.model.edt.chosen_nodes["zephyr,my-bus"].path == i2c.path
    # Names and targets are checked.
    assert not s.set_property("/aliases", "Bad_Name", f"&{label}", overlay).ok
    assert not s.set_property("/aliases", "x", "&nope", overlay).ok
    # And entries can be removed again.
    r = s.set_property("/aliases", "my-bus", None, overlay, delete=True)
    assert r.ok, r.message
    assert "\tmy-bus" not in overlay.read_text()


def test_chosen_names():
    from pathlib import Path
    import tempfile
    from lazyzephyr.dts import catalog
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "drv.c").write_text("#if DT_HAS_CHOSEN(zephyr_shell_uart)\n"
                                    "DEVICE_DT_GET(DT_CHOSEN(nordic_pm_ext_flash))\n")
        (root / "Kconfig").write_text("DT_CHOSEN_Z_RAM_CONSOLE := zephyr,ram-console\n")
        assert catalog.chosen_names([root]) == {"zephyr,shell-uart", "nordic,pm-ext-flash",
                                                "zephyr,ram-console"}
