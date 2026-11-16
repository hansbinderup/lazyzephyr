"""The two ways to install an SDK: Nix (zephyr-nix) and `west sdk install`.

Both download the same SDK release from GitHub and give the same folder layout,
so the toolchain list comes from that release's files either way.
"""

from __future__ import annotations

import json
import re
import shutil
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..builder.runner import west_command
from .model import host_platform, hosttools_path, is_gnu_subdir_layout, sdk_path

# A fixed zephyr-nix commit, so an install doesn't change under you. Bump it to get
# newer SDK versions (docs/development.md); `zephyr_nix` in the settings overrides it.
ZEPHYR_NIX = "github:nix-community/zephyr-nix/5ef0903c5fcdbbce55af2732fad1011e90b6ff47"
NIX = ["--extra-experimental-features", "nix-command flakes"]
RELEASE_API = "https://api.github.com/repos/zephyrproject-rtos/sdk-ng/releases/tags/v{version}"
_NAME = re.compile(r"[A-Za-z0-9_.+-]+")


@dataclass
class Availability:
    ok: bool
    reason: str


# -- availability ------------------------------------------------------------


def short_flake(flake: str) -> str:
    """github:owner/repo/<40-char rev> shown as repo@<7 chars>."""
    m = re.fullmatch(r"github:[^/]+/([^/]+)/([0-9a-f]{40})", flake)
    return f"{m.group(1)}@{m.group(2)[:7]}" if m else flake


def nix_available(nix: list[str] | None = None, flake: str = ZEPHYR_NIX) -> Availability:
    if nix is None and shutil.which("nix") is None:
        return Availability(
            False, "nix not found on PATH. Install it from https://nixos.org/download"
        )
    system, _ = host_platform()
    if system not in ("linux", "macos"):
        return Availability(False, "zephyr-nix only supports Linux and macOS")
    return Availability(True, f"uses {short_flake(flake)}")


def west_sdk_script(zephyr_base: Path | None) -> Path | None:
    if zephyr_base is None:
        return None
    script = Path(zephyr_base) / "scripts" / "west_commands" / "sdk.py"
    return script if script.is_file() else None


def west_available(zephyr_base: Path | None, west: list[str] | None = None) -> Availability:
    if (west or west_command()) is None:
        return Availability(
            False, "west not found: p sets up the workspace's Python environment with it"
        )
    if west_sdk_script(zephyr_base) is None:
        return Availability(False, "this Zephyr has no `west sdk` (it needs Zephyr 4.0 or later)")
    return Availability(True, "Zephyr's own installer")


# -- toolchains --------------------------------------------------------------


def release_cache(cache_dir: Path, version: str) -> Path:
    return cache_dir / "sdk-releases" / f"v{version}.json"


def release_assets(version: str, cache_dir: Path, timeout: float = 15) -> list[str]:
    """File names of an SDK release on GitHub. Releases don't change, so they're cached."""
    cache = release_cache(cache_dir, version)
    try:
        names = json.loads(cache.read_text())
        if isinstance(names, list) and all(isinstance(n, str) for n in names):
            return names
    except (OSError, ValueError):
        pass
    request = urllib.request.Request(
        RELEASE_API.format(version=version),
        headers={"Accept": "application/vnd.github+json", "User-Agent": "lazyzephyr"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise LookupError(f"there is no Zephyr SDK {version} release on GitHub") from None
        raise LookupError(f"could not get the SDK {version} release from GitHub: {exc}") from None
    except (OSError, ValueError) as exc:
        raise LookupError(f"could not get the SDK {version} release from GitHub: {exc}") from None
    names = [a["name"] for a in data.get("assets", []) if isinstance(a.get("name"), str)]
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(names))
    except OSError:
        pass
    return names


def toolchains_in_release(
    names: list[str], version: str, host: tuple[str, str] | None = None
) -> list[str]:
    """The GNU toolchains a release has for this host, from its file names."""
    system, arch = host or host_platform()
    gnu = "gnu_" if is_gnu_subdir_layout(version) else ""
    prefix = f"toolchain_{gnu}{system}-{arch}_"
    found = {
        re.sub(r"\.(tar\.xz|tar\.gz|7z|zip)$", "", n[len(prefix) :])
        for n in names
        if n.startswith(prefix)
    }
    return sorted(t for t in found if n_ok(t))


def n_ok(name: str) -> bool:
    return bool(_NAME.fullmatch(name))


# -- west --------------------------------------------------------------------


def west_toolchain_flag(zephyr_base: Path | None) -> str:
    """`--gnu-toolchains` from Zephyr 4.4 on, `--toolchains` before."""
    script = west_sdk_script(zephyr_base)
    try:
        text = script.read_text() if script else ""
    except OSError:
        text = ""
    return "--gnu-toolchains" if '"--gnu-toolchains"' in text else "--toolchains"


def west_install_args(
    version: str, store: Path, toolchains: list[str], hosttools: bool, flag: str
) -> list[str]:
    """Arguments to `west` that install (or add toolchains to) an SDK in the store."""
    args = ["sdk", "install", "--version", version, "--install-base", str(store)]
    if toolchains:
        args += [flag, *toolchains]
    else:
        args.append("--no-gnu-toolchains" if flag == "--gnu-toolchains" else "--no-toolchains")
    if not hosttools:
        args.append("--no-hosttools")
    return args


# -- nix ---------------------------------------------------------------------


def _nix_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${") + '"'


def _packages(flake: str) -> str:
    return f"(builtins.getFlake {_nix_string(flake)}).packages.${{builtins.currentSystem}}"


def nix_versions_command(flake: str = ZEPHYR_NIX) -> list[str]:
    """Prints {"sdk-1_0": "1.0.1", ...}: the SDK versions zephyr-nix packages."""
    expr = (
        f"let p = {_packages(flake)}; "
        'names = builtins.filter (n: builtins.match "sdk-[0-9_]+" n != null) '
        "(builtins.attrNames p); "
        "in builtins.listToAttrs (map (n: { name = n; value = p.${n}.version; }) names)"
    )
    return ["nix", *NIX, "eval", "--impure", "--json", "--expr", expr]


def nix_attr_for(versions: dict[str, str], version: str) -> str | None:
    """The zephyr-nix attribute that has exactly this SDK version."""
    matches = sorted(attr for attr, v in versions.items() if v == version)
    return matches[0] if matches else None


def nix_install_commands(
    attr: str,
    version: str,
    store: Path,
    toolchains: list[str],
    hosttools: bool,
    flake: str = ZEPHYR_NIX,
) -> list[list[str]]:
    """`nix build` commands linking the SDK (and its host tools) into the store."""
    bad = [t for t in [attr, *toolchains] if not n_ok(t)]
    if bad:
        raise ValueError(f"invalid name: {bad[0]!r}")
    targets = " ".join(_nix_string(t) for t in toolchains)
    sdk_expr = f'{_packages(flake)}."{attr}".override {{ targets = [ {targets} ]; }}'
    build = ["nix", *NIX, "build", "--impure", "--print-build-logs"]
    commands = [[*build, "--out-link", str(sdk_path(store, version)), "--expr", sdk_expr]]
    if hosttools:
        tools_attr = attr.replace("sdk-", "hosttools-", 1)
        tools_expr = f'{_packages(flake)}."{tools_attr}"'
        commands.append(
            [*build, "--out-link", str(hosttools_path(store, version)), "--expr", tools_expr]
        )
    return commands


# -- removing ----------------------------------------------------------------


def _inside(path: Path, store: Path) -> bool:
    return path.parent.resolve() == store.resolve() or path.resolve().is_relative_to(
        store.resolve()
    )


def remove_sdk(store: Path, version: str) -> None:
    """Remove one SDK version from the store: a Nix link, or a west folder."""
    for path in (sdk_path(store, version), hosttools_path(store, version)):
        if path.is_symlink():
            path.unlink()
        elif path.is_dir() and _inside(path, store):
            shutil.rmtree(path)


def remove_hosttools(store: Path, version: str) -> bool:
    """Delete the host tools from a west-installed SDK in the store."""
    sdk = sdk_path(store, version)
    if sdk.is_symlink() or not sdk.is_dir():
        return False
    target = sdk / ("hosttools" if is_gnu_subdir_layout(version) else "sysroots")
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
        return True
    return False


def remove_toolchains(store: Path, version: str, toolchains: list[str]) -> list[str]:
    """Delete toolchains from a west-installed SDK in the store. Returns what was removed."""
    sdk = sdk_path(store, version)
    if sdk.is_symlink() or not sdk.is_dir():
        return []
    base = sdk / "gnu" if is_gnu_subdir_layout(version) else sdk
    removed = []
    for name in toolchains:
        target = base / name
        if n_ok(name) and target.is_dir() and not target.is_symlink() and _inside(target, store):
            shutil.rmtree(target)
            removed.append(name)
    return removed
