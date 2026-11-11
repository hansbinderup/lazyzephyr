import textwrap
from pathlib import Path

from lazyzephyr.builder.configs import (BuildConfig, ConfigStore, build_cmd, build_dir,
                                         config_from_build, default_build_dir, runner_cmd,
                                         target_cmd)
from lazyzephyr.dts.workspace import Workspace


def ws(tmp_path) -> Workspace:
    (tmp_path / ".west").mkdir(exist_ok=True)
    (tmp_path / ".west" / "config").write_text("[zephyr]\nbase = zephyr\n")
    return Workspace(tmp_path, tmp_path / "zephyr")


def test_build_command(tmp_path):
    w = ws(tmp_path)
    cfg = BuildConfig("x", "apps/blinky", "nrf9160dk/nrf9160/ns", revision="0.14.0",
                      shields=["a", "b"], snippets=["rtt-console"], sysbuild=False,
                      extra_conf=["debug.conf"], extra_overlays=["x.overlay"],
                      cmake_args=["-DCONFIG_LOG=y"])
    cmd = build_cmd(w, cfg, pristine=True)
    d = str(tmp_path / "build-blinky-nrf9160dk_nrf9160_ns")
    assert cmd == ["build", "-d", d, "-b", "nrf9160dk@0.14.0/nrf9160/ns", "-p", "always",
                   "--no-sysbuild", "--shield", "a", "--shield", "b", "-S", "rtt-console",
                   str(tmp_path / "apps/blinky"), "--",
                   "-DEXTRA_CONF_FILE=debug.conf", "-DEXTRA_DTC_OVERLAY_FILE=x.overlay",
                   "-DCONFIG_LOG=y"]
    assert target_cmd(w, cfg, "menuconfig") == ["build", "-d", d, "-t", "menuconfig"]


def test_runner_command(tmp_path):
    w = ws(tmp_path)
    cfg = BuildConfig("x", "app", "b", runner="jlink", dev_id="123", flash_args=["--erase"],
                      build_dir="out")
    assert runner_cmd(w, cfg) == ["flash", "-d", str(tmp_path / "out"), "-r", "jlink",
                                  "--dev-id", "123", "--erase"]
    assert runner_cmd(w, cfg, "debug") == ["debug", "-d", str(tmp_path / "out"), "-r",
                                           "jlink", "--dev-id", "123"]


def test_default_dir_follows_west_dir_fmt(tmp_path):
    w = ws(tmp_path)
    cfg = BuildConfig("x", "apps/blinky", "native_sim/native/64")
    assert default_build_dir(w, cfg) == tmp_path / "build-blinky-native_sim_native_64"
    (tmp_path / ".west" / "config").write_text("[build]\ndir-fmt = out/{app}/{board}\n")
    assert build_dir(w, cfg) == tmp_path / "out/blinky/native_sim/native/64"


def test_import_from_build_info(tmp_path):
    w = ws(tmp_path)
    build = tmp_path / ".build_ui"
    build.mkdir()
    (build / "build_info.yml").write_text(textwrap.dedent(f"""\
        cmake:
          application:
            source-dir: '{tmp_path}/applications/app'
          board:
            name: 'myboard'
            qualifiers: 'mysoc'
            revision: 'A'
        west:
          command: '/nix/bin/west build -b myboard applications/app --no-sysbuild --shield=s1 --shield=s2 -d .build_ui -p -- -DOVERLAY_CONFIG=ui.conf'
        """))
    cfg = config_from_build(w, build)
    assert cfg.app == "applications/app"
    assert cfg.board == "myboard/mysoc"
    assert cfg.revision is None  # board default, not asked for
    assert cfg.shields == ["s1", "s2"]
    assert cfg.sysbuild is False
    assert cfg.build_dir == ".build_ui"
    assert cfg.cmake_args == ["-DOVERLAY_CONFIG=ui.conf"]


def test_store_round_trip(tmp_path):
    path = tmp_path / "builds.json"
    a = ConfigStore(tmp_path / "ws1", path)
    a.configs.append(BuildConfig("one", "app", "b", shields=["s"]))
    a.save()
    b = ConfigStore(tmp_path / "ws2", path)
    b.configs.append(BuildConfig("two", "app", "b"))
    b.save()
    again = ConfigStore(tmp_path / "ws1", path)
    assert [c.name for c in again.configs] == ["one"] and again.configs[0].shields == ["s"]
    assert again.unique_name("one") == "one (2)"


def test_import_sysbuild(tmp_path):
    w = ws(tmp_path)
    build = tmp_path / "sb"
    (build / "app").mkdir(parents=True)
    (build / "domains.yaml").write_text("default: app\n")
    (build / "CMakeCache.txt").write_text(f"APP_DIR:PATH={tmp_path}/apps/real\n")
    (build / "build_info.yml").write_text(textwrap.dedent(f"""\
        cmake:
          application:
            source-dir: '{tmp_path}/zephyr/share/sysbuild'
          board:
            name: 'b'
        west:
          command: 'west build -b b --sysbuild -d sb'
        """))
    from lazyzephyr.builder.configs import dts_build_dir, is_sysbuild_image
    cfg = config_from_build(w, build)
    assert cfg.app == "apps/real" and cfg.sysbuild is True and cfg.name == "sb"
    assert is_sysbuild_image(build / "app") and not is_sysbuild_image(build)
    assert dts_build_dir(build) == build / "app"
