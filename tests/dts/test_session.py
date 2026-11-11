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
