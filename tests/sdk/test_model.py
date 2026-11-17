"""SDK version, installed SDKs, settings and the build environment."""

from pathlib import Path

from lazyzephyr.sdk.model import (
    Settings,
    active_sdk,
    hosttools_path,
    inspect_sdk,
    required_version,
    sdk_env,
    sdk_path,
    store_dir,
    store_sdks,
)


def make_sdk(path: Path, version: str, toolchains=(), hosttools=False) -> Path:
    path.mkdir(parents=True)
    (path / "sdk_version").write_text(version + "\n")
    base = path / "gnu" if version.startswith("1.") else path
    for tc in toolchains:
        gcc = base / tc / "bin" / f"{tc}-gcc"
        gcc.parent.mkdir(parents=True)
        gcc.write_text("")
    if hosttools:
        (path / ("hosttools" if version.startswith("1.") else "sysroots")).mkdir()
    return path


def zephyr(tmp_path: Path, version="1.0.1") -> Path:
    base = tmp_path / "zephyr"
    base.mkdir()
    (base / "SDK_VERSION").write_text(version + "\n")
    return base


def test_required_version(tmp_path):
    assert required_version(zephyr(tmp_path)) == "1.0.1"
    assert required_version(tmp_path / "nowhere") is None
    assert required_version(None) is None
    (tmp_path / "zephyr" / "SDK_VERSION").write_text("garbage\n")
    assert required_version(tmp_path / "zephyr") is None


def test_store_dir_follows_the_environment(tmp_path, monkeypatch):
    assert store_dir() == tmp_path / "sdk-store"  # set by conftest
    monkeypatch.delenv("LAZYZEPHYR_SDK_DIR")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert store_dir() == tmp_path / "data" / "lazyzephyr" / "sdk"


def test_inspect_both_layouts(tmp_path):
    new = inspect_sdk(make_sdk(tmp_path / "a", "1.0.1", ["arm-zephyr-eabi"], hosttools=True))
    assert new.version == "1.0.1" and new.toolchains == ["arm-zephyr-eabi"] and new.hosttools
    old = inspect_sdk(make_sdk(tmp_path / "b", "0.17.4", ["x86_64-zephyr-elf", "arm-zephyr-eabi"]))
    assert old.toolchains == ["arm-zephyr-eabi", "x86_64-zephyr-elf"] and not old.hosttools
    assert inspect_sdk(tmp_path / "missing") is None


def test_store_tells_west_folders_from_nix_links(tmp_path):
    store = tmp_path / "store"
    make_sdk(sdk_path(store, "0.17.4"), "0.17.4", ["arm-zephyr-eabi"])
    built = make_sdk(tmp_path / "nix" / "abc-zephyr-sdk", "1.0.1", ["riscv64-zephyr-elf"])
    sdk_path(store, "1.0.1").symlink_to(built)
    found = {s.version: s.backend for s in store_sdks(store)}
    assert found == {"0.17.4": "west", "1.0.1": "nix"}


def test_settings_round_trip_and_bad_files(tmp_path):
    s = Settings(backend="nix", toolchains=["b", "a", "a"], hosttools=False, use=False)
    s.save(tmp_path)
    again = Settings.load(tmp_path)
    assert (again.backend, again.toolchains, again.hosttools, again.use) == (
        "nix",
        ["a", "b"],
        False,
        False,
    )
    (tmp_path / "lazyzephyr-sdk.json").write_text('{"backend": "apt", "toolchains": 3}')
    bad = Settings.load(tmp_path)
    assert bad.backend == "west" and bad.toolchains == [] and bad.use and bad.guide


def test_builds_get_the_sdk_this_zephyr_needs(tmp_path):
    store, base = tmp_path / "store", zephyr(tmp_path)
    assert "ZEPHYR_SDK_INSTALL_DIR" not in sdk_env({}, base, store)  # not installed yet
    make_sdk(sdk_path(store, "1.0.1"), "1.0.1", ["arm-zephyr-eabi"])
    (hosttools_path(store, "1.0.1") / "bin").mkdir(parents=True)
    env = sdk_env({"PATH": "/usr/bin"}, base, store)
    assert env["ZEPHYR_SDK_INSTALL_DIR"] == str(sdk_path(store, "1.0.1"))
    assert env["ZEPHYR_TOOLCHAIN_VARIANT"] == "zephyr"
    assert env["PATH"] == f"{hosttools_path(store, '1.0.1') / 'bin'}:/usr/bin"
    assert active_sdk(base, store) == (str(sdk_path(store, "1.0.1")), "installed by lazyzephyr")


def test_the_users_own_sdk_and_the_off_switch_win(tmp_path, monkeypatch):
    store, base = tmp_path / "store", zephyr(tmp_path)
    make_sdk(sdk_path(store, "1.0.1"), "1.0.1")
    mine = {"ZEPHYR_SDK_INSTALL_DIR": "/opt/mine"}
    assert sdk_env(mine, base, store) == mine
    Settings(use=False).save(store)
    assert "ZEPHYR_SDK_INSTALL_DIR" not in sdk_env({}, base, store)
    assert active_sdk(base, store)[0] == ""
    monkeypatch.setenv("ZEPHYR_SDK_INSTALL_DIR", "/opt/mine")
    assert active_sdk(base, store) == (
        "/opt/mine",
        "ZEPHYR_SDK_INSTALL_DIR is set in your environment",
    )


async def test_build_jobs_get_the_sdk(tmp_path, monkeypatch):
    import os
    import sys

    from lazyzephyr.builder.runner import Job, Runner

    store, base = Path(os.environ["LAZYZEPHYR_SDK_DIR"]), zephyr(tmp_path)
    make_sdk(sdk_path(store, "1.0.1"), "1.0.1", ["arm-zephyr-eabi"])
    lines = []
    show = "import os; print(os.environ.get('ZEPHYR_SDK_INSTALL_DIR'))"
    runner = Runner(tmp_path, lambda job, line: lines.append(line), lambda job: None)
    runner.zephyr_base = base
    job = await runner.run(Job("t", "c", "build", [["-c", show]], program=[sys.executable]))
    assert job.status == "passed" and lines[-1] == str(sdk_path(store, "1.0.1"))
