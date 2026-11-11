"""Find the west workspace, its Zephyr modules, boards and build directories,
and work out which devicetree inputs a target uses.

Mirrors what Zephyr's CMake does in pre_dt.cmake, dts.cmake, shields.cmake and
configuration_files.cmake, close enough to parse the devicetree without a build.
An existing build's build_info.yml is exact, so it is preferred when available.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# Directories never descended into while scanning.
SKIP_DIRS = frozenset({".git", ".west", ".venv", "venv", "__pycache__", "node_modules", "build",
                       "twister-out"})
SKIP_PREFIXES = ("build-", "build_", "twister-out", ".")


def _skip(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(SKIP_PREFIXES)


def load_yaml(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.load(f, Loader=_Loader)
    except (OSError, yaml.YAMLError):
        return None


# --------------------------------------------------------------------------
# Workspace and modules
# --------------------------------------------------------------------------


@dataclass
class Module:
    name: str
    path: Path
    dts_root: Path | None = None
    board_root: Path | None = None


@dataclass
class Workspace:
    topdir: Path
    zephyr_base: Path
    modules: list[Module] = field(default_factory=list)

    @property
    def dts_roots(self) -> list[Path]:
        return [m.dts_root for m in self.modules if m.dts_root]

    @property
    def board_roots(self) -> list[Path]:
        return [m.board_root for m in self.modules if m.board_root]

    def is_upstream(self, path: Path) -> bool:
        """Files we don't offer to edit: Zephyr itself and third-party modules."""
        path = Path(path).resolve()
        if path.is_relative_to(self.zephyr_base):
            return True
        return "modules" in path.relative_to(self.topdir).parts[:-1] \
            if path.is_relative_to(self.topdir) else True


def find_topdir(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        if (directory / ".west").is_dir():
            return directory
    return None


def find_workspace(start: Path, zephyr_base: Path | None = None) -> Workspace:
    """The west workspace around `start`. Raises LookupError if there is none."""
    start = start.resolve()
    topdir = find_topdir(start)
    if zephyr_base is None:
        env = os.environ.get("ZEPHYR_BASE")
        if env and Path(env).is_dir():
            zephyr_base = Path(env)
    if zephyr_base is None and topdir:
        cfg = configparser.ConfigParser()
        cfg.read(topdir / ".west" / "config")
        base = cfg.get("zephyr", "base", fallback="zephyr")
        if (topdir / base).is_dir():
            zephyr_base = topdir / base
    if zephyr_base is None:
        raise LookupError("no west workspace or ZEPHYR_BASE found")
    zephyr_base = zephyr_base.resolve()
    topdir = (topdir or zephyr_base.parent).resolve()
    return Workspace(topdir, zephyr_base, find_modules(topdir, zephyr_base))


def find_modules(topdir: Path, zephyr_base: Path, max_depth: int = 4) -> list[Module]:
    """Every directory with a zephyr/module.yml (or zephyr/module.yaml) under topdir."""
    modules = []
    for dirpath, dirnames, _ in os.walk(topdir):
        here = Path(dirpath)
        depth = len(here.relative_to(topdir).parts)
        dirnames[:] = sorted(d for d in dirnames if not _skip(d)) if depth < max_depth else []
        if here == zephyr_base:
            dirnames.clear()
            continue
        for name in ("module.yml", "module.yaml"):
            meta_file = here / "zephyr" / name
            if meta_file.is_file():
                modules.append(_module(here, meta_file))
                break
    return modules


def _module(path: Path, meta_file: Path) -> Module:
    meta = load_yaml(meta_file) or {}
    settings = (meta.get("build") or {}).get("settings") or {}
    module = Module(meta.get("name") or path.name, path)
    if settings.get("dts_root"):
        module.dts_root = (path / settings["dts_root"]).resolve()
    if settings.get("board_root"):
        module.board_root = (path / settings["board_root"]).resolve()
    return module


# --------------------------------------------------------------------------
# Boards and shields
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Board:
    """One buildable board target, e.g. nrf5340dk/nrf5340/cpuapp/ns."""

    name: str
    # SoC first, then cpucluster and variants: ("nrf5340", "cpuapp", "ns").
    quals: tuple[str, ...]
    directory: Path
    single_soc: bool
    vendor: str | None = None
    full_name: str | None = None

    @property
    def target(self) -> str:
        return "/".join((self.name, *self.quals))

    @property
    def qualifiers(self) -> str:
        return "/".join(self.quals)

    @property
    def build_string(self) -> str:
        return "_".join((self.name, *self.quals))

    @property
    def short_build_string(self) -> str:
        return "_".join((self.name, *self.quals[1:]))


def _board_roots(ws: Workspace) -> list[Path]:
    return [ws.zephyr_base, *ws.board_roots]


def find_soc_clusters(ws: Workspace) -> dict[str, list[str]]:
    """SoC name -> its cpuclusters, from every soc.yml."""
    clusters: dict[str, list[str]] = {}

    def walk(entry):
        if isinstance(entry, dict):
            if "name" in entry and "cpuclusters" in entry:
                clusters[entry["name"]] = [c["name"] for c in entry["cpuclusters"] or []]
            for v in entry.values():
                walk(v)
        elif isinstance(entry, list):
            for v in entry:
                walk(v)

    for root in [ws.zephyr_base, *(m.path for m in ws.modules)]:
        base = root / "soc"
        if base.is_dir():
            for soc_yml in base.rglob("soc.yml"):
                walk(load_yaml(soc_yml))
    return clusters


def find_boards(ws: Workspace) -> list[Board]:
    """Every board target that has its own devicetree."""
    clusters = find_soc_clusters(ws)
    boards = []
    for root in _board_roots(ws):
        base = root / "boards"
        if not base.is_dir():
            continue
        for board_yml in sorted(base.rglob("board.yml")):
            boards.extend(b for b in _boards_from_yml(board_yml, clusters) if _has_dts(b))
    return sorted(boards, key=lambda b: b.target)


def _variants(variants, cluster: str | None, prefix: tuple[str, ...]) -> list[tuple[str, ...]]:
    out = []
    for v in variants or []:
        if not isinstance(v, dict) or "name" not in v:
            continue
        if v.get("cpucluster") not in (None, cluster):
            continue
        quals = (*prefix, str(v["name"]))
        out.append(quals)
        out += _variants(v.get("variants"), cluster, quals)
    return out


def _boards_from_yml(board_yml: Path, clusters: dict[str, list[str]]) -> list[Board]:
    data = load_yaml(board_yml) or {}
    entries = data.get("boards") or ([data["board"]] if data.get("board") else [])
    boards = []
    for entry in entries:
        if not isinstance(entry, dict) or "name" not in entry:
            continue
        socs = entry.get("socs") or []
        for soc in socs:
            for cluster in clusters.get(soc["name"]) or [None]:
                base = (soc["name"], cluster) if cluster else (soc["name"],)
                for quals in [base, *_variants(soc.get("variants"), cluster, base)]:
                    boards.append(Board(entry["name"], quals, board_yml.parent, len(socs) == 1,
                                        entry.get("vendor"), entry.get("full_name")))
    return boards


def _has_dts(board: Board) -> bool:
    try:
        board_dts(board)
        return True
    except LookupError:
        return False


def find_board(ws: Workspace, target: str) -> Board:
    """`name`, `name/soc` or `name/soc/variant`. Raises LookupError."""
    name, _, rest = target.partition("/")
    candidates = [b for b in find_boards(ws) if b.name == name]
    if not candidates:
        raise LookupError(f"unknown board: {name}")
    if not rest:
        plain = [b for b in candidates if len(b.quals) == 1]
        if len(plain) == 1:
            return plain[0]
        raise LookupError(f"board {name} has several SoCs; use one of: "
                          + ", ".join(b.target for b in plain))
    for board in candidates:
        if board.qualifiers == rest:
            return board
    raise LookupError(f"unknown target {target}; known: "
                      + ", ".join(b.target for b in candidates))


def find_shield(ws: Workspace, name: str) -> Path:
    for root in _board_roots(ws):
        base = root / "boards" / "shields"
        if not base.is_dir():
            continue
        for overlay in base.rglob(f"{name}.overlay"):
            return overlay.parent
    raise LookupError(f"unknown shield: {name}")


# --------------------------------------------------------------------------
# Devicetree inputs
# --------------------------------------------------------------------------


@dataclass
class DtInputs:
    """Everything needed to preprocess and parse one target's devicetree."""

    description: str
    files: list[Path]
    include_dirs: list[Path]
    bindings_dirs: list[Path]
    # Overlays the user owns (app overlays), in order. The default place to write.
    user_files: list[Path] = field(default_factory=list)
    compiler: str | None = None
    workdir: Path | None = None
    build_dir: Path | None = None


def _arch_dirs(root: Path) -> list[str]:
    dts = root / "dts"
    if not dts.is_dir():
        return []
    skip = {"common", "vendor", "bindings"}
    return sorted((p.name for p in dts.iterdir() if p.is_dir() and p.name not in skip),
                  reverse=True)


def _include_dirs(roots: list[Path]) -> list[Path]:
    dirs: list[Path] = []
    for root in roots:
        for rel in ("include", "include/zephyr", "dts/common", "dts/vendor",
                    *(f"dts/{a}" for a in _arch_dirs(root)), "dts"):
            path = (root / rel).resolve()
            if path.is_dir() and path not in dirs:
                dirs.append(path)
    return dirs


def _bindings_dirs(roots: list[Path]) -> list[Path]:
    return [r / "dts" / "bindings" for r in roots if (r / "dts" / "bindings").is_dir()]


def _dedup(paths: list[Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        p = p.resolve()
        if p not in out:
            out.append(p)
    return out


def board_dts(board: Board) -> Path:
    for name in (board.build_string, board.short_build_string if board.single_soc else None):
        if name and (board.directory / f"{name}.dts").is_file():
            return board.directory / f"{name}.dts"
    raise LookupError(f"no devicetree for {board.target} in {board.directory}")


def app_overlays(app: Path, board: Board) -> list[Path]:
    names = [board.short_build_string, board.build_string, board.name]
    found = []
    for directory in (app / "boards", app):
        found = [directory / f"{n}.overlay" for n in dict.fromkeys(names)
                 if (directory / f"{n}.overlay").is_file()]
        if found:
            return found
    return [app / "app.overlay"] if (app / "app.overlay").is_file() else []


def inputs_for_board(ws: Workspace, board: Board, app: Path | None = None,
                     shields: list[str] = ()) -> DtInputs:
    shield_dirs = [find_shield(ws, s) for s in shields]
    roots = _dedup([*ws.dts_roots, *([app] if app else []), board.directory, *shield_dirs,
                    ws.zephyr_base])
    files = [board_dts(board)]
    for name, directory in zip(shields, shield_dirs):
        files.append(directory / f"{name}.overlay")
        for n in (board.short_build_string, board.build_string, board.name):
            extra = directory / "boards" / f"{n}.overlay"
            if extra.is_file() and extra not in files:
                files.append(extra)
    user = app_overlays(app, board) if app else []
    files += user
    desc = board.target + (f" + {', '.join(shields)}" if shields else "")
    if app:
        desc += f"  ({_rel(app, ws.topdir)})"
    return DtInputs(desc, files, _include_dirs(roots), _bindings_dirs(roots), user,
                    workdir=app)


def inputs_from_build(build_dir: Path) -> DtInputs:
    """Read build_info.yml of an existing build. Raises LookupError."""
    info = load_yaml(build_dir / "build_info.yml")
    try:
        cmake = info["cmake"]
        dt = cmake["devicetree"]
    except (TypeError, KeyError):
        raise LookupError(f"no devicetree info in {build_dir}/build_info.yml") from None
    board = cmake.get("board", {})
    target = "/".join(x for x in (board.get("name"), board.get("qualifiers")) if x)
    app = cmake.get("application", {}).get("source-dir")
    compiler = None
    cache = build_dir / "CMakeCache.txt"
    if cache.is_file():
        for line in cache.read_text(errors="replace").splitlines():
            if line.startswith("CMAKE_C_COMPILER:"):
                compiler = line.split("=", 1)[1].strip() or None
                break
    return DtInputs(
        f"{target}  (build: {build_dir.name})",
        [Path(f).resolve() for f in dt.get("files", [])],
        [Path(d) for d in dt.get("include-dirs", [])],
        [Path(d) for d in dt.get("bindings-dirs", [])],
        [Path(f).resolve() for f in (dt.get("user-files") or []) + (dt.get("extra-user-files")
                                                                    or [])],
        compiler=compiler,
        workdir=Path(app) if app else None,
        build_dir=build_dir,
    )


def find_build_dirs(ws: Workspace, start: Path, max_depth: int = 3) -> list[Path]:
    """Build directories (with build_info.yml) under the workspace and `start`."""
    found: list[Path] = []
    for base in dict.fromkeys([start.resolve(), ws.topdir]):
        for dirpath, dirnames, filenames in os.walk(base):
            here = Path(dirpath)
            depth = len(here.relative_to(base).parts)
            if "build_info.yml" in filenames and (here / "zephyr").is_dir():
                if here not in found:
                    found.append(here)
            dirnames[:] = [d for d in dirnames
                           if depth < max_depth and d not in {".git", ".west", "zephyr",
                                                              "modules", "__pycache__", ".venv"}]
    return found


def _rel(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)
