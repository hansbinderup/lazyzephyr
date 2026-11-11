"""Load a build's Kconfig tree with Zephyr's own kconfiglib.

Kconfig needs files CMake generates (Kconfig.modules, Kconfig.dts, board and
SoC directories…), so it is read from an existing build directory: its
`menuconfig` target spells out the environment Kconfig runs with, and
build_info.yml lists the conf files that make up the configuration.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shlex
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
# kconfiglib reads os.environ while parsing; one load at a time.
_ENV_LOCK = threading.Lock()


class KconfigError(Exception):
    """The build's Kconfig can't be found or doesn't parse."""


@dataclass
class BuildKconfig:
    """How to load one build's Kconfig."""

    build_dir: Path
    kconfig_file: Path  # the root Kconfig file (zephyr/Kconfig)
    env: dict[str, str]
    conf_files: list[Path]  # merged in this order, like Zephyr's kconfig.py
    user_files: list[Path]  # the application's own (prj.conf, boards/*.conf…)
    board: str = ""

    @property
    def zephyr_base(self) -> Path:
        return Path(self.env.get("ZEPHYR_BASE") or self.env["srctree"])

    @property
    def dot_config(self) -> Path:
        return Path(self.env.get("KCONFIG_CONFIG") or self.build_dir / "zephyr" / ".config")


def _menuconfig_command(build_dir: Path) -> list[str]:
    """The command line of the build's menuconfig target (Ninja or Makefiles)."""
    candidates = [build_dir / "build.ninja",
                  build_dir / "zephyr" / "CMakeFiles" / "menuconfig.dir" / "build.make",
                  build_dir / "CMakeFiles" / "menuconfig.dir" / "build.make"]
    for path in candidates:
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if "menuconfig.py" in line and "-E env" in line:
                cmd = line.strip()
                cmd = re.sub(r"^\s*COMMAND\s*=\s*", "", cmd)
                cmd = cmd.split(" && ", 1)[1] if cmd.startswith("cd ") and " && " in cmd \
                    else cmd
                try:
                    return shlex.split(cmd)
                except ValueError:
                    continue
    raise KconfigError(f"{build_dir} has no menuconfig target; build it first")


def find(build_dir: Path) -> BuildKconfig:
    """Everything needed to load `build_dir`'s Kconfig. Raises KconfigError."""
    build_dir = build_dir.resolve()
    tokens = _menuconfig_command(build_dir)
    env: dict[str, str] = {}
    kconfig_file = None
    start = tokens.index("env") + 1 if "env" in tokens else 0
    for i, tok in enumerate(tokens[start:], start):
        if "=" in tok and not tok.startswith("-") and re.match(r"^[A-Za-z_]\w*=", tok):
            key, _, value = tok.partition("=")
            env[key] = value
        elif tok.endswith("menuconfig.py"):
            rest = tokens[i + 1:]
            kconfig_file = Path(rest[0]) if rest else None
            break
    if not env.get("srctree") or kconfig_file is None:
        raise KconfigError(f"can't read the Kconfig environment of {build_dir}")
    try:
        with open(build_dir / "build_info.yml", encoding="utf-8") as f:
            info = yaml.load(f, Loader=_Loader) or {}
    except (OSError, yaml.YAMLError):
        info = {}
    kconf = ((info.get("cmake") or {}).get("kconfig") or {}) if isinstance(info, dict) else {}
    conf_files = [Path(p).resolve() for p in kconf.get("files") or []]
    user_files = [Path(p).resolve() for p in kconf.get("user-files") or []]
    board = ((info.get("cmake") or {}).get("board") or {}) if isinstance(info, dict) else {}
    target = "/".join(x for x in (board.get("name"), board.get("qualifiers")) if x)
    return BuildKconfig(build_dir, kconfig_file, env, conf_files, user_files, target)


def import_kconfiglib(zephyr_base: Path):
    for sub in ("scripts/kconfig", "scripts/dts/python-devicetree/src"):
        p = str(zephyr_base / sub)
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module("kconfiglib")


@dataclass
class Trace:
    """Why a symbol got its value in the last build (.config-trace.json)."""
    kind: str  # assign, default, select, imply, unset
    where: str = ""  # "file:line" or the select/imply condition


@dataclass
class Model:
    build: BuildKconfig
    kconf: object
    kconfiglib: object
    built: dict[str, str] = field(default_factory=dict)  # symbol -> value in .config
    trace: dict[str, Trace] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def symbol(self, name: str):
        return self.kconf.syms.get(name.removeprefix("CONFIG_"))


def load(build: BuildKconfig) -> Model:
    """Parse the Kconfig tree and evaluate the conf files. Raises KconfigError."""
    lib = import_kconfiglib(build.zephyr_base)
    with _ENV_LOCK:
        saved = dict(os.environ)
        os.environ.update(build.env)
        try:
            kconf = lib.Kconfig(str(build.kconfig_file), warn=False, warn_to_stderr=False)
        except Exception as exc:  # KconfigError, OSError, …
            raise KconfigError(f"Kconfig doesn't parse: {exc}") from None
        finally:
            os.environ.clear()
            os.environ.update(saved)
    model = Model(build, kconf, lib)
    model.built = read_config(build.dot_config)
    model.trace = read_trace(build.build_dir / "zephyr" / ".config-trace.json")
    evaluate(model)
    return model


def evaluate(model: Model) -> None:
    """Apply the conf files in order, as Zephyr's kconfig.py does."""
    kconf = model.kconf
    del kconf.warnings[:]
    kconf.warn = True
    kconf.warn_to_stderr = False
    files = [f for f in model.build.conf_files if f.is_file()]
    if not files:
        kconf.load_config(str(model.build.dot_config))
    else:
        kconf.load_config(str(files[0]))
        for f in files[1:]:
            kconf.load_config(str(f), replace=False)
    model.warnings = list(kconf.warnings)


_CONFIG_LINE = re.compile(r"^(?:CONFIG_(\w+)=(.*)|# CONFIG_(\w+) is not set)\s*$")


def read_config(path: Path) -> dict[str, str]:
    """Symbol (without CONFIG_) -> value as written, `n` for 'is not set'."""
    out = {}
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        m = _CONFIG_LINE.match(line.strip())
        if m:
            if m.group(1):
                value = m.group(2)
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
                out[m.group(1)] = value
            else:
                out[m.group(3)] = "n"
    return out


def read_trace(path: Path) -> dict[str, Trace]:
    try:
        rows = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    out = {}
    for row in rows if isinstance(rows, list) else ():
        try:
            name, _, _, _, kind, loc = row[:6]
        except (TypeError, ValueError):
            continue
        if isinstance(loc, list) and len(loc) == 2 and isinstance(loc[1], int):
            where = f"{loc[0]}:{loc[1]}"
        elif isinstance(loc, list) and loc:
            where = str(loc[0])
        else:
            where = ""
        out[str(name).removeprefix("CONFIG_")] = Trace(kind, where)
    return out
