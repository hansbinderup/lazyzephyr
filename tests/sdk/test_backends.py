"""Install commands for west sdk and Nix, and the toolchain list."""

import json

import pytest

from lazyzephyr.sdk import backends
from lazyzephyr.sdk.model import hosttools_path, sdk_path

from .test_model import make_sdk

ASSETS_1_0 = [
    "zephyr-sdk-1.0.1_linux-x86_64_minimal.tar.xz",
    "toolchain_gnu_linux-x86_64_arm-zephyr-eabi.tar.xz",
    "toolchain_gnu_linux-x86_64_riscv64-zephyr-elf.tar.xz",
    "toolchain_gnu_linux-aarch64_arm-zephyr-eabi.tar.xz",
    "toolchain_gnu_windows-x86_64_arm-zephyr-eabi.7z",
    "toolchain_llvm_linux-x86_64.tar.xz",
    "hosttools_linux-x86_64.tar.xz",
]


def test_toolchains_for_this_host_only():
    host = ("linux", "x86_64")
    assert backends.toolchains_in_release(ASSETS_1_0, "1.0.1", host) == [
        "arm-zephyr-eabi",
        "riscv64-zephyr-elf",
    ]
    old = ["toolchain_macos-aarch64_xtensa-espressif_esp32_zephyr-elf.tar.xz"]
    assert backends.toolchains_in_release(old, "0.17.4", ("macos", "aarch64")) == [
        "xtensa-espressif_esp32_zephyr-elf"
    ]


def test_release_list_comes_from_the_cache(tmp_path, monkeypatch):
    cache = backends.release_cache(tmp_path, "1.0.1")
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps(ASSETS_1_0))

    def no_network(*a, **k):
        raise AssertionError("asked GitHub")

    monkeypatch.setattr(backends.urllib.request, "urlopen", no_network)
    assert backends.release_assets("1.0.1", tmp_path) == ASSETS_1_0


def test_west_args_follow_the_zephyr_version(tmp_path):
    new = tmp_path / "new" / "scripts" / "west_commands"
    new.mkdir(parents=True)
    (new / "sdk.py").write_text('add_argument(\n    "--gnu-toolchains",\n)')
    old = tmp_path / "old" / "scripts" / "west_commands"
    old.mkdir(parents=True)
    (old / "sdk.py").write_text('add_argument(\n    "--toolchains",\n)')
    assert backends.west_toolchain_flag(tmp_path / "new") == "--gnu-toolchains"
    assert backends.west_toolchain_flag(tmp_path / "old") == "--toolchains"
    args = backends.west_install_args(
        "0.17.4", tmp_path, ["arm-zephyr-eabi"], False, "--toolchains"
    )
    assert args == [
        "sdk",
        "install",
        "--version",
        "0.17.4",
        "--install-base",
        str(tmp_path),
        "--toolchains",
        "arm-zephyr-eabi",
        "--no-hosttools",
    ]
    assert backends.west_install_args("1.0.1", tmp_path, [], True, "--gnu-toolchains")[-1] == (
        "--no-gnu-toolchains"
    )


def test_west_needs_a_zephyr_with_west_sdk(tmp_path):
    assert not backends.west_available(tmp_path, ["west"]).ok
    script = tmp_path / "scripts" / "west_commands" / "sdk.py"
    script.parent.mkdir(parents=True)
    script.write_text("")
    assert backends.west_available(tmp_path, ["west"]).ok


def test_nix_picks_the_exact_version_and_links_into_the_store(tmp_path):
    versions = {"sdk-0_17": "0.17.4", "sdk-1_0": "1.0.1"}
    assert backends.nix_attr_for(versions, "1.0.1") == "sdk-1_0"
    assert backends.nix_attr_for(versions, "0.17.3") is None
    sdk, tools = backends.nix_install_commands(
        "sdk-1_0", "1.0.1", tmp_path, ["arm-zephyr-eabi"], hosttools=True
    )
    assert sdk[sdk.index("--out-link") + 1] == str(sdk_path(tmp_path, "1.0.1"))
    expr = sdk[sdk.index("--expr") + 1]
    assert '."sdk-1_0".override { targets = [ "arm-zephyr-eabi" ]; }' in expr
    assert f"(builtins.getFlake {json.dumps(backends.ZEPHYR_NIX)})" in expr
    assert backends.short_flake(backends.ZEPHYR_NIX).startswith("zephyr-nix@")
    assert tools[tools.index("--out-link") + 1] == str(hosttools_path(tmp_path, "1.0.1"))
    assert '."hosttools-1_0"' in tools[tools.index("--expr") + 1]
    with pytest.raises(ValueError):
        backends.nix_install_commands("sdk-1_0", "1.0.1", tmp_path, ['x"; evil'], False)


def test_removing_stays_inside_the_store(tmp_path):
    store = tmp_path / "store"
    make_sdk(sdk_path(store, "1.0.1"), "1.0.1", ["arm-zephyr-eabi", "riscv64-zephyr-elf"])
    assert backends.remove_toolchains(store, "1.0.1", ["arm-zephyr-eabi", "../../x", "nope"]) == [
        "arm-zephyr-eabi"
    ]
    assert (sdk_path(store, "1.0.1") / "gnu" / "riscv64-zephyr-elf").is_dir()
    outside = make_sdk(tmp_path / "nix-out", "0.17.4")
    sdk_path(store, "0.17.4").symlink_to(outside)
    backends.remove_sdk(store, "0.17.4")  # a Nix link: only the link goes
    assert outside.is_dir() and not sdk_path(store, "0.17.4").exists()
    backends.remove_sdk(store, "1.0.1")
    assert not sdk_path(store, "1.0.1").exists()
