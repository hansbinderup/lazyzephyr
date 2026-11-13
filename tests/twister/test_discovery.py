from pathlib import Path

from lazyzephyr.twister.discovery import default_roots, discover, find_workspace


def test_discovers_scenarios_and_merges_common(test_tree: Path):
    found = discover([test_tree])
    names = [s.name for s in found.scenarios]
    assert names == ["drivers.beta", "lib.alpha.default", "lib.alpha.variant"]
    variant = found.by_name()["lib.alpha.variant"]
    assert variant.build_only
    assert variant.tags == ["lib", "alpha", "variant"]
    assert variant.platform_allow == ["native_sim", "qemu_x86"]
    assert variant.integration_platforms == ["native_sim"]
    assert not found.by_name()["lib.alpha.default"].build_only


def test_reports_broken_yaml_and_skips_twister_out(test_tree: Path):
    found = discover([test_tree])
    assert list(found.errors) == [test_tree / "broken" / "testcase.yaml"]
    assert "bogus.scenario" not in found.by_name()


def test_ztest_cases_match_twister_ids(test_tree: Path):
    alpha = discover([test_tree]).by_name()["lib.alpha.default"]
    ids = [c.id for c in alpha.cases]
    assert ids == ["alpha_suite.adds", "alpha_suite.with_fixture", "alpha_user.plain_name"]
    first = alpha.cases[0]
    assert first.filter == "alpha_suite::test_adds"
    assert first.line == 3
    assert alpha.suites == ["alpha_suite", "alpha_user"]


def test_under_and_platforms(test_tree: Path):
    found = discover([test_tree])
    assert [s.name for s in found.under(test_tree / "drivers")] == ["drivers.beta"]
    assert found.platforms() == ["native_sim", "qemu_x86"]


def test_fixtures_from_harness_config(tmp_path: Path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "testcase.yaml").write_text(
        "common:\n"
        "  harness_config:\n"
        "    fixture: uart_loopback\n"
        "tests:\n"
        "  t.common: {}\n"
        "  t.own:\n"
        "    harness_config: {fixture: gpio_loopback}\n"
        "  t.none:\n"
        "    harness_config: {}\n"
        "    harness: console\n"
    )
    found = discover([tmp_path])
    by_name = found.by_name()
    assert by_name["t.common"].fixtures == ["uart_loopback"]
    assert by_name["t.own"].fixtures == ["gpio_loopback"]
    assert by_name["t.none"].fixtures == ["uart_loopback"]  # an empty mapping inherits
    assert found.fixtures() == {"gpio_loopback": 1, "uart_loopback": 2}


def test_find_workspace(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    (tmp_path / ".west").mkdir()
    (tmp_path / ".west" / "config").write_text("[zephyr]\nbase = zephyr\n")
    nested = tmp_path / "app" / "tests"
    nested.mkdir(parents=True)
    ws = find_workspace(nested)
    assert ws.topdir == tmp_path
    assert ws.zephyr_base == tmp_path / "zephyr"


def test_default_roots(tmp_path: Path):
    assert default_roots(tmp_path) == [tmp_path.resolve()]
    (tmp_path / "tests").mkdir()
    assert default_roots(tmp_path) == [(tmp_path / "tests").resolve()]
