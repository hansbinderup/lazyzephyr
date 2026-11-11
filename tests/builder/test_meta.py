import textwrap

from lazyzephyr.builder import meta
from lazyzephyr.dts.workspace import Board


def test_revisions_and_runners(tmp_path):
    d = tmp_path / "myboard"
    d.mkdir()
    (d / "board.yml").write_text(textwrap.dedent("""\
        board:
          name: myboard
          socs:
            - name: mysoc
          revision:
            format: letter
            default: "B"
            revisions:
              - name: "A"
              - name: "B"
        """))
    (d / "board.cmake").write_text(textwrap.dedent("""\
        board_runner_args(jlink "--device=X")
        include(${ZEPHYR_BASE}/boards/common/openocd-stm32.board.cmake)
        include(${ZEPHYR_BASE}/boards/common/jlink.board.cmake)
        board_set_flasher_ifnset(jlink)
        """))
    board = Board("myboard", ("mysoc",), d, True)
    revs = meta.board_revisions(board)
    assert revs.default == "B" and revs.names == ("A", "B")
    runners = meta.board_runners(board)
    assert runners.names == ("openocd", "jlink") and runners.flash_default == "jlink"
    # A build's runners.yaml wins.
    build = tmp_path / "build"
    (build / "zephyr").mkdir(parents=True)
    (build / "zephyr" / "runners.yaml").write_text("runners: [pyocd]\nflash-runner: pyocd\n")
    assert meta.board_runners(board, build).names == ("pyocd",)


def test_probes_from_sysfs(tmp_path):
    def dev(name, vendor, serial, product):
        p = tmp_path / name
        p.mkdir()
        (p / "idVendor").write_text(vendor + "\n")
        (p / "serial").write_text(serial + "\n")
        (p / "product").write_text(product + "\n")
    dev("1-1", "1366", "000123456789", "J-Link")
    dev("1-2", "0483", "ABC", "STM32 STLink")
    dev("1-3", "0403", "FT1", "FT232R USB UART")   # plain serial cable: skipped
    dev("1-4", "046d", "X", "Mouse")
    probes = meta.find_probes(tmp_path)
    assert [(p.kind, p.dev_id) for p in probes] == [("J-Link", "123456789"), ("ST-Link", "ABC")]


def test_shields(tmp_path, monkeypatch):
    from lazyzephyr.dts.workspace import Workspace
    zb = tmp_path / "zephyr"
    s1 = zb / "boards" / "shields" / "one"
    s1.mkdir(parents=True)
    (s1 / "shield.yml").write_text("shields:\n  - name: one_a\n    full_name: One A\n"
                                   "  - name: one_b\n")
    s2 = zb / "boards" / "shields" / "two"
    s2.mkdir(parents=True)
    (s2 / "two.overlay").write_text("")
    names = [s.name for s in meta.find_shields(Workspace(tmp_path, zb))]
    assert names == ["one_a", "one_b", "two"]
