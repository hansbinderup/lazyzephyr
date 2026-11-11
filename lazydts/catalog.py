"""Every devicetree binding available to a target, and which ones have a driver."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# Bindings only used by Zephyr's own tests.
_SKIP_DIRS = {"test"}

_DRIVER_RE = re.compile(
    r"DT_DRV_COMPAT\s+(\w+)|DT_HAS_COMPAT_STATUS_OKAY\(\s*(\w+)|"
    r"DT_FOREACH_STATUS_OKAY\(\s*(\w+)|DT_HAS_(\w+)_ENABLED"
)


@dataclass(frozen=True)
class BindingInfo:
    compatible: str
    path: Path
    description: str
    on_bus: str | None
    buses: tuple[str, ...]
    has_driver: bool = False

    @property
    def summary(self) -> str:
        for line in self.description.splitlines():
            if line.strip():
                return line.strip()
        return ""


def compat_token(compat: str) -> str:
    """`vnd,foo-bar` -> `vnd_foo_bar`, as used in DT_DRV_COMPAT."""
    return re.sub(r"[^a-z0-9]", "_", compat.lower())


def binding_files(bindings_dirs: list[Path]) -> list[Path]:
    out = []
    for d in bindings_dirs:
        for dirpath, dirnames, filenames in os.walk(d):
            dirnames[:] = [n for n in dirnames if n not in _SKIP_DIRS]
            out += [Path(dirpath) / f for f in filenames if f.endswith((".yaml", ".yml"))]
    return out


def _load(path: Path):
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.load(f, Loader=_Loader)
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _includes(raw: dict) -> list[str]:
    inc = raw.get("include")
    items = inc if isinstance(inc, list) else ([inc] if inc else [])
    return [i if isinstance(i, str) else i.get("name") for i in items
            if isinstance(i, str) or isinstance(i, dict) and i.get("name")]


def scan_bindings(bindings_dirs: list[Path], drivers: set[str] | None = None) -> list[BindingInfo]:
    files = binding_files(bindings_dirs)
    by_name = {}
    for f in files:
        by_name.setdefault(f.name, f)
    raws = {f: _load(f) for f in files}

    def inherited(raw: dict, key: str, seen: set) -> object:
        if raw.get(key) is not None:
            return raw[key]
        for name in _includes(raw):
            inc = by_name.get(name)
            if inc and inc not in seen and raws.get(inc):
                seen.add(inc)
                val = inherited(raws[inc], key, seen)
                if val is not None:
                    return val
        return None

    out = []
    for path, raw in raws.items():
        if not raw or not isinstance(raw.get("compatible"), str):
            continue
        bus = inherited(raw, "bus", set())
        buses = tuple(bus) if isinstance(bus, list) else ((bus,) if bus else ())
        compat = raw["compatible"]
        out.append(BindingInfo(
            compat, path, str(raw.get("description") or raw.get("title") or ""),
            inherited(raw, "on-bus", set()), buses,
            drivers is not None and compat_token(compat) in drivers,
        ))
    return sorted(out, key=lambda b: (b.compatible, b.on_bus or ""))


def driver_tokens(roots: list[Path]) -> set[str]:
    """Compatibles (as tokens) that some C code or Kconfig in `roots` uses."""
    roots = [r for r in roots if r.is_dir()]
    if not roots:
        return set()
    text = ""
    grep = shutil.which("rg") or shutil.which("grep")
    if grep:
        pattern = r"(DT_DRV_COMPAT|DT_HAS_COMPAT_STATUS_OKAY|DT_FOREACH_STATUS_OKAY)"
        if grep.endswith("rg"):
            cmd = [grep, "--no-filename", "-o", "-e", pattern + r"[\s(]*\w+", "-e", r"DT_HAS_\w+_ENABLED",
                   "-g", "*.{c,h,cpp,cc}", "-g", "Kconfig*", *map(str, roots)]
        else:
            cmd = [grep, "-rhoE", "--include=*.c", "--include=*.h", "--include=*.cpp",
                   "--include=*.cc", "--include=Kconfig*",
                   "-e", pattern + r"[[:space:](]*[A-Za-z0-9_]+",
                   "-e", r"DT_HAS_[A-Za-z0-9_]+_ENABLED", *map(str, roots)]
        try:
            text = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
        except (OSError, subprocess.TimeoutExpired):
            text = ""
    tokens = set()
    for m in _DRIVER_RE.finditer(text):
        token = next(g for g in m.groups() if g)
        tokens.add(token.lower())
    return tokens


def driver_roots(ws) -> list[Path]:
    """Where drivers live: Zephyr's code and the workspace's own modules."""
    roots = [ws.zephyr_base / d for d in ("drivers", "subsys", "soc", "arch", "lib", "modules")]
    for m in ws.modules:
        for p in (m.path, m.dts_root):
            if p and not ws.is_upstream(p / "x") and p not in roots:
                roots.append(p)
    return roots
