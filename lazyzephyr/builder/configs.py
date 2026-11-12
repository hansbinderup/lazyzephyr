"""Saved build configurations and the west commands they turn into."""

from __future__ import annotations

import argparse
import configparser
import json
import os
import re
import shlex
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from ..dts.workspace import Workspace, load_yaml


@dataclass
class BuildConfig:
    name: str
    app: str                       # relative to the workspace top dir, or absolute
    board: str                     # target, e.g. nrf5340dk/nrf5340/cpuapp
    revision: str | None = None
    shields: list[str] = field(default_factory=list)
    snippets: list[str] = field(default_factory=list)
    sysbuild: bool | None = None   # None: west's default (build.sysbuild)
    build_dir: str | None = None   # None: the default for this app + board
    extra_conf: list[str] = field(default_factory=list)      # EXTRA_CONF_FILE
    extra_overlays: list[str] = field(default_factory=list)  # EXTRA_DTC_OVERLAY_FILE
    cmake_args: list[str] = field(default_factory=list)      # after `--`
    runner: str | None = None      # None: the board's default
    dev_id: str | None = None
    flash_args: list[str] = field(default_factory=list)
    monitor: str = ""              # shell command for the serial monitor
    # Last action and how it went, shown in the list.
    last_action: str | None = None
    last_status: str | None = None
    last_time: float | None = None

    @classmethod
    def from_dict(cls, data: dict) -> BuildConfig:
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})

    @property
    def target(self) -> str:
        """`board@revision/qualifiers` as west build -b wants it."""
        if not self.revision:
            return self.board
        name, sep, quals = self.board.partition("/")
        return f"{name}@{self.revision}{sep}{quals}"


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------


def app_path(ws: Workspace, cfg: BuildConfig) -> Path:
    p = Path(cfg.app)
    return p if p.is_absolute() else ws.topdir / p


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_")


def west_dir_fmt(ws: Workspace) -> str | None:
    """west's build.dir-fmt, from the workspace, user or system config."""
    paths = [Path("/etc/westconfig"),
             Path(os.environ.get("WEST_CONFIG_GLOBAL", Path.home() / ".westconfig")),
             ws.topdir / ".west" / "config"]
    value = None
    for p in paths:
        cfg = configparser.ConfigParser()
        try:
            cfg.read(p)
        except configparser.Error:
            continue
        value = cfg.get("build", "dir-fmt", fallback=value)
    return value


def default_build_dir(ws: Workspace, cfg: BuildConfig) -> Path:
    app = app_path(ws, cfg)
    board = cfg.board.replace("/", "_")
    fmt = west_dir_fmt(ws)
    if fmt:
        try:
            rel_src = os.path.relpath(app, ws.topdir)
            path = fmt.format(board=cfg.board, app=app.name, source_dir=rel_src,
                              west_topdir=str(ws.topdir))
            return (ws.topdir / path).resolve()
        except (KeyError, IndexError, ValueError):
            pass
    return ws.topdir / f"build-{_slug(app.name)}-{_slug(board)}"


def build_dir(ws: Workspace, cfg: BuildConfig) -> Path:
    if cfg.build_dir:
        p = Path(cfg.build_dir)
        return p if p.is_absolute() else ws.topdir / p
    return default_build_dir(ws, cfg)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def build_cmd(ws: Workspace, cfg: BuildConfig, pristine: bool = False,
              target: str | None = None) -> list[str]:
    """`west build` arguments (without the west executable)."""
    args = ["build", "-d", str(build_dir(ws, cfg)), "-b", cfg.target]
    if pristine:
        args += ["-p", "always"]
    if cfg.sysbuild is True:
        args.append("--sysbuild")
    elif cfg.sysbuild is False:
        args.append("--no-sysbuild")
    for s in cfg.shields:
        args += ["--shield", s]
    for s in cfg.snippets:
        args += ["-S", s]
    if target:
        args += ["-t", target]
    args.append(str(app_path(ws, cfg)))
    cmake = []
    if cfg.extra_conf:
        cmake.append("-DEXTRA_CONF_FILE=" + ";".join(cfg.extra_conf))
    if cfg.extra_overlays:
        cmake.append("-DEXTRA_DTC_OVERLAY_FILE=" + ";".join(cfg.extra_overlays))
    cmake += cfg.cmake_args
    if cmake:
        args += ["--", *cmake]
    return args


def target_cmd(ws: Workspace, cfg: BuildConfig, target: str) -> list[str]:
    """`west build -t <target>` on the existing build (menuconfig, run, rom_report…)."""
    return ["build", "-d", str(build_dir(ws, cfg)), "-t", target]


def runner_cmd(ws: Workspace, cfg: BuildConfig, command: str = "flash",
               extra: list[str] = ()) -> list[str]:
    """`west flash|debug|attach|debugserver|rtt` arguments."""
    args = [command, "-d", str(build_dir(ws, cfg))]
    if cfg.runner:
        args += ["-r", cfg.runner]
    if cfg.dev_id:
        args += ["--dev-id", cfg.dev_id]
    if command == "flash":
        args += cfg.flash_args
    args += list(extra)
    return args


def monitor_env(ws: Workspace, cfg: BuildConfig) -> dict[str, str]:
    """Environment for the monitor command: $BUILD_DIR and $BOARD point at this config."""
    return dict(os.environ, BUILD_DIR=str(build_dir(ws, cfg)), BOARD=cfg.target,
                TERM="xterm-256color")


def shown(args: list[str]) -> str:
    return "west " + shlex.join(args)


# --------------------------------------------------------------------------
# Importing an existing build
# --------------------------------------------------------------------------


def _west_build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False, exit_on_error=False)
    p.add_argument("-b", "--board")
    p.add_argument("-d", "--build-dir")
    p.add_argument("-p", "--pristine", nargs="?", const="always")
    p.add_argument("--shield", action="append", default=[])
    p.add_argument("-S", "--snippet", action="append", default=[])
    p.add_argument("--sysbuild", action="store_true", default=None)
    p.add_argument("--no-sysbuild", dest="sysbuild", action="store_false")
    p.add_argument("-t", "--target")
    p.add_argument("source_dir", nargs="?")
    return p


def config_from_build(ws: Workspace, build: Path) -> BuildConfig | None:
    """A config that rebuilds `build`, from its build_info.yml."""
    info = load_yaml(build / "build_info.yml")
    if not isinstance(info, dict):
        return None
    cmake = info.get("cmake") or {}
    board = cmake.get("board") or {}
    app = (cmake.get("application") or {}).get("source-dir")
    target = "/".join(x for x in (board.get("name"), board.get("qualifiers")) if x)
    command = (info.get("west") or {}).get("command") or ""
    parsed = argparse.Namespace(shield=[], snippet=[], sysbuild=None, board=None,
                                source_dir=None)
    cmake_args: list[str] = []
    try:
        words = shlex.split(command)
        if "build" in words:
            words = words[words.index("build") + 1:]
            if "--" in words:
                cmake_args = words[words.index("--") + 1:]
                words = words[:words.index("--")]
            parsed, _ = _west_build_parser().parse_known_args(words)
    except (ValueError, argparse.ArgumentError):
        pass
    board_arg = parsed.board or target
    revision = board.get("revision") or None
    if "@" in board_arg:
        name, _, rest = board_arg.partition("@")
        rev, sep, quals = rest.partition("/")
        board_arg, revision = name + sep + quals, rev
    # Prefer the full target from build_info (has the qualifiers west filled in).
    if target and target.split("/")[0] == board_arg.split("/")[0]:
        board_arg = target
    if parsed.source_dir:
        # The command names the real app, also for sysbuild (whose build_info
        # source-dir is Zephyr's share/sysbuild).
        src = Path(parsed.source_dir)
        app = str(src if src.is_absolute() else ws.topdir / src)
    elif app and Path(app).parts[-2:] == ("share", "sysbuild"):
        app = _cmake_cache(build).get("APP_DIR") or app
    if not app:
        return None
    app_rel = _rel(Path(app).resolve(), ws.topdir)
    shields = list(dict.fromkeys(s for arg in parsed.shield for s in arg.split(",") if s))
    name = _rel(build.resolve(), ws.topdir)
    cfg = BuildConfig(
        name=name,
        app=app_rel, board=board_arg, revision=None,
        shields=shields, snippets=list(parsed.snippet), sysbuild=parsed.sysbuild,
        build_dir=_rel(build.resolve(), ws.topdir), cmake_args=cmake_args,
    )
    # Keep an explicit revision only if it was asked for (not the board default).
    if revision and "@" in (parsed.board or ""):
        cfg.revision = revision
    return cfg


def _cmake_cache(build: Path) -> dict[str, str]:
    out = {}
    try:
        for line in (build / "CMakeCache.txt").read_text(errors="replace").splitlines():
            if ":" in line and "=" in line and not line.startswith(("#", "//")):
                key, _, rest = line.partition(":")
                out[key] = rest.partition("=")[2]
    except OSError:
        pass
    return out


def is_sysbuild_image(build: Path) -> bool:
    """A domain image inside a sysbuild build directory."""
    return (build.parent / "domains.yaml").is_file()


def dts_build_dir(build: Path) -> Path:
    """The build to show in the Devicetree tab: for sysbuild, the default image."""
    domains = load_yaml(build / "domains.yaml")
    if isinstance(domains, dict) and domains.get("default"):
        return build / str(domains["default"])
    return build


def _rel(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


def store_file() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "lazyzephyr" / "builds.json"


class ConfigStore:
    """Build configs per workspace, in ~/.cache/lazyzephyr/builds.json."""

    def __init__(self, topdir: Path, path: Path | None = None):
        self.key = str(topdir)
        self.path = path or store_file()
        self.configs: list[BuildConfig] = []
        self.load()

    def _all(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def load(self) -> None:
        raw = self._all().get(self.key) or []
        self.configs = []
        for item in raw:
            try:
                self.configs.append(BuildConfig.from_dict(item))
            except TypeError:
                continue

    def save(self) -> None:
        data = self._all()
        data[self.key] = [asdict(c) for c in self.configs]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)

    def unique_name(self, name: str, ignore: BuildConfig | None = None) -> str:
        taken = {c.name for c in self.configs if c is not ignore}
        if name not in taken:
            return name
        i = 2
        while f"{name} ({i})" in taken:
            i += 1
        return f"{name} ({i})"
