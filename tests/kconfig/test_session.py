from lazyzephyr.kconfig import describe, loader
from lazyzephyr.kconfig.app import search
from lazyzephyr.kconfig.session import Session, set_in_text


def make(kbuild):
    s = Session(loader.find(kbuild))
    s.load()
    return s


def test_find_reads_the_build(kbuild):
    b = loader.find(kbuild)
    assert b.kconfig_file.name == "Kconfig" and b.board == "test_board"
    assert [f.name for f in b.conf_files] == ["test_defconfig", "prj.conf"]
    assert [f.name for f in b.user_files] == ["prj.conf"]


def test_values_and_problems(kbuild):
    s = make(kbuild)
    assert s.symbol("SHELL").str_value == "y"
    assert s.symbol("NAME").str_value == "board"
    assert s.symbol("I2C_SHELL").str_value == "n"
    assert s.pending() == []
    [p] = s.problems()
    assert "prj.conf:1: CONFIG_I2C_SHELL=y has no effect" in p.message and "I2C" in p.message


def test_turn_on_with_dependencies(kbuild):
    s = make(kbuild)
    prj = kbuild.parent / "prj.conf"
    r = s.set_values([("I2C_SHELL", "y")])
    assert not r.ok and r.needs == ["I2C"]
    assert prj.read_text() == "CONFIG_I2C_SHELL=y\n"  # rolled back
    r = s.set_values([("I2C", "y"), ("I2C_SHELL", "y")])
    assert r.ok, r.message
    assert prj.read_text() == "CONFIG_I2C_SHELL=y\nCONFIG_I2C=y\n"
    assert s.symbol("I2C_SHELL").str_value == "y"
    assert s.problems() == []
    # Not built yet: the last build had them off.
    assert {name for name, *_ in s.pending()} >= {"I2C", "I2C_SHELL"}
    assert s.undo().ok and prj.read_text() == "CONFIG_I2C_SHELL=y\n"


def test_ranges_selects_choices(kbuild):
    s = make(kbuild)
    assert s.set_values([("I2C", "y")]).ok
    r = s.set_values([("SPEED", "5000")])
    assert not r.ok and "out of range" in r.message and "1–1000" in r.message
    assert s.set_values([("SPEED", "400")]).ok
    assert s.set_values([("SENSOR", "y")]).ok
    r = s.set_values([("I2C", "n")])
    assert not r.ok and "selected by SENSOR" in r.message
    assert s.set_values([("LOG_MODE_IMMEDIATE", "y")]).ok
    assert s.symbol("LOG_MODE_DEFERRED").str_value == "n"
    r = s.set_values([("HIDDEN_HELPER", "y")])
    assert not r.ok and "can't be set directly" in r.message
    assert s.set_values([("NAME", 'my "dev"')]).ok
    assert 'CONFIG_NAME="my \\"dev\\""' in (kbuild.parent / "prj.conf").read_text()
    r = s.remove("SPEED")
    assert r.ok and s.symbol("SPEED").str_value == "100"


def test_set_in_text():
    assert set_in_text("A=1\nCONFIG_X=y\n", "X", "CONFIG_X=n") == "A=1\nCONFIG_X=n\n"
    assert set_in_text("# CONFIG_X is not set\n", "X", "CONFIG_X=y") == "CONFIG_X=y\n"
    assert set_in_text("CONFIG_X=y\nCONFIG_Y=y\nCONFIG_X=n\n", "X", None) == "CONFIG_Y=y\n"
    assert set_in_text("", "X", "CONFIG_X=y") == "CONFIG_X=y\n"


def test_describe_and_search(kbuild):
    s = make(kbuild)
    text = describe.details(s, s.symbol("I2C_SHELL"), kbuild.parent.parent).plain
    assert "has no effect" in text and "Depends on (not met)" in text
    assert "also turn on: I2C" in text
    rel = [(k, n) for k, n, _ in describe.related(s, s.symbol("I2C_SHELL"))]
    assert ("depends on", "I2C") in rel and ("depends on", "SHELL") in rel
    text = describe.details(s, s.symbol("SERIAL") or s.symbol("SHELL"), None).plain
    assert "Default y" in text
    assert [x.name for x in search(s, "shell")][:2] == ["SHELL", "I2C_SHELL"]
    assert [x.name for x in search(s, "bus speed")] == ["SPEED"]
