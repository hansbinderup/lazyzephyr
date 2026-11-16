"""What SDK a Zephyr tree needs, what is installed, and where SDKs are kept.

lazyzephyr keeps the SDKs it installs in one folder shared by every workspace
(see store_dir). Each version is a `zephyr-sdk-<version>` entry there, laid out
like `west sdk install` lays it out: a real folder (west) or a link into the
Nix store (Nix). Builds and twister runs started by lazyzephyr then get
ZEPHYR_SDK_INSTALL_DIR pointing at the version the workspace needs.
"""

from __future__ import annotations

import json
import os
import platform
import re
from dataclasses import dataclass, field
from pathlib import Path

SETTINGS_FILE = "lazyzephyr-sdk.json"
BACKENDS = ("nix", "west")


def store_dir() -> Path:
    """The folder shared by every workspace: $LAZYZEPHYR_SDK_DIR, else under XDG data."""
    if os.environ.get("LAZYZEPHYR_SDK_DIR"):
        return Path(os.environ["LAZYZEPHYR_SDK_DIR"]).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "lazyzephyr" / "sdk"


def required_version(zephyr_base: Path | None) -> str | None:
    """The SDK version the Zephyr tree asks for, from its SDK_VERSION file."""
    if zephyr_base is None:
        return None
    try:
        text = (Path(zephyr_base) / "SDK_VERSION").read_text().strip()
    except OSError:
        return None
    first = text.splitlines()[0].strip() if text else ""
    return first if re.fullmatch(r"\d+\.\d+\.\d+", first) else None


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version)[:3])


def is_gnu_subdir_layout(version: str) -> bool:
    """SDK 1.0 and later keep the GNU toolchains under gnu/."""
    return version_key(version) >= (1, 0, 0)


def host_platform() -> tuple[str, str]:
    """The SDK's names for this host: (linux|macos|windows, x86_64|aarch64)."""
    system = {"Darwin": "macos", "Windows": "windows"}.get(platform.system(), "linux")
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    return system, arch


# -- installed SDKs ----------------------------------------------------------


@dataclass
class InstalledSdk:
    version: str
    path: Path
    backend: str  # "nix", "west" (a folder in our store) or "other" (outside it)
    toolchains: list[str] = field(default_factory=list)
    hosttools: bool = False

    @property
    def in_store(self) -> bool:
        return self.backend in BACKENDS


def _toolchains_in(sdk: Path, version: str) -> list[str]:
    base = sdk / "gnu" if is_gnu_subdir_layout(version) else sdk
    try:
        entries = sorted(base.iterdir())
    except OSError:
        return []
    return [e.name for e in entries if (e / "bin" / f"{e.name}-gcc").exists()]


def inspect_sdk(path: Path, backend: str = "other") -> InstalledSdk | None:
    """An SDK folder's version and toolchains, or None if it isn't one."""
    try:
        version = (path / "sdk_version").read_text().strip().splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return InstalledSdk(
        version=version,
        path=path,
        backend=backend,
        toolchains=_toolchains_in(path, version),
        hosttools=(path / "hosttools").is_dir() or (path / "sysroots").is_dir(),
    )


def sdk_path(store: Path, version: str) -> Path:
    return store / f"zephyr-sdk-{version}"


def hosttools_path(store: Path, version: str) -> Path:
    """Where the Nix backend links the SDK's host tools (a separate package there)."""
    return store / f"hosttools-{version}"


def store_sdks(store: Path) -> list[InstalledSdk]:
    out = []
    try:
        entries = sorted(store.glob("zephyr-sdk-*"))
    except OSError:
        return []
    for entry in entries:
        # west installs a folder; Nix leaves a link into its store.
        backend = "nix" if entry.is_symlink() else "west"
        sdk = inspect_sdk(entry, backend)
        if sdk is not None:
            out.append(sdk)
    return out


def elsewhere(store: Path) -> list[InstalledSdk]:
    """SDKs outside our store that Zephyr's own lookup would find (not the CMake registry)."""
    home = Path.home()
    roots = [home, home / ".local", home / ".local" / "opt", home / "bin", Path("/opt")]
    roots += [Path("/usr/local"), Path("/usr")]
    seen, out = set(), []
    env_dir = os.environ.get("ZEPHYR_SDK_INSTALL_DIR")
    candidates = [Path(env_dir)] if env_dir else []
    for root in roots:
        try:
            candidates += sorted(root.glob("zephyr-sdk-*"))
        except OSError:
            continue
    for path in candidates:
        try:
            real = path.resolve()
        except OSError:
            continue
        if real in seen or real.is_relative_to(store.resolve()):
            continue
        seen.add(real)
        sdk = inspect_sdk(path)
        if sdk is not None:
            out.append(sdk)
    return out


# -- settings ----------------------------------------------------------------


@dataclass
class Settings:
    """Choices made in the SDK tab, shared by every workspace (kept in the store)."""

    backend: str | None = None  # "nix" or "west"; None until chosen
    toolchains: list[str] = field(default_factory=list)
    hosttools: bool = True
    use: bool = True  # point builds at our SDK
    zephyr_nix: str | None = None  # a flake to use instead of the pinned zephyr-nix

    @classmethod
    def load(cls, store: Path) -> Settings:
        try:
            data = json.loads((store / SETTINGS_FILE).read_text())
        except (OSError, ValueError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        backend = data.get("backend")
        toolchains = data.get("toolchains")
        return cls(
            backend=backend if backend in BACKENDS else None,
            toolchains=[t for t in toolchains if isinstance(t, str)]
            if isinstance(toolchains, list)
            else [],
            hosttools=data.get("hosttools") is not False,
            use=data.get("use") is not False,
            zephyr_nix=data["zephyr_nix"] if isinstance(data.get("zephyr_nix"), str) else None,
        )

    def save(self, store: Path) -> None:
        store.mkdir(parents=True, exist_ok=True)
        tmp = store / (SETTINGS_FILE + ".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "backend": self.backend,
                    "toolchains": sorted(set(self.toolchains)),
                    "hosttools": self.hosttools,
                    "use": self.use,
                    **({"zephyr_nix": self.zephyr_nix} if self.zephyr_nix else {}),
                },
                indent=2,
            )
        )
        tmp.replace(store / SETTINGS_FILE)


# -- environment for builds --------------------------------------------------


def sdk_env(env: dict[str, str], zephyr_base: Path | None, store: Path | None = None) -> dict:
    """`env` plus what points builds at our SDK for this Zephyr tree, if we have it.

    An SDK the user picked themselves (ZEPHYR_SDK_INSTALL_DIR already set) wins.
    """
    env = dict(env)
    if env.get("ZEPHYR_SDK_INSTALL_DIR"):
        return env
    version = required_version(zephyr_base)
    if version is None:
        return env
    store = store or store_dir()
    if not Settings.load(store).use:
        return env
    sdk = sdk_path(store, version)
    if inspect_sdk(sdk) is None:
        return env
    env["ZEPHYR_SDK_INSTALL_DIR"] = str(sdk)
    env.setdefault("ZEPHYR_TOOLCHAIN_VARIANT", "zephyr")
    tools = hosttools_path(store, version) / "bin"
    if tools.is_dir():
        env["PATH"] = os.pathsep.join(p for p in (str(tools), env.get("PATH", "")) if p)
    return env


def active_sdk(zephyr_base: Path | None, store: Path) -> tuple[str, str]:
    """Which SDK lazyzephyr's builds will use, and why: (path or "", reason)."""
    env_dir = os.environ.get("ZEPHYR_SDK_INSTALL_DIR")
    if env_dir:
        return env_dir, "ZEPHYR_SDK_INSTALL_DIR is set in your environment"
    env = sdk_env({}, zephyr_base, store)
    if "ZEPHYR_SDK_INSTALL_DIR" in env:
        return env["ZEPHYR_SDK_INSTALL_DIR"], "installed by lazyzephyr"
    if not Settings.load(store).use:
        return "", "off: Zephyr looks for an SDK itself"
    return "", "none from lazyzephyr: Zephyr looks for an SDK itself"
