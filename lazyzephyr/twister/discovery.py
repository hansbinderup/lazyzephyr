"""Discover the west workspace and the twister test scenarios inside it."""

from __future__ import annotations

import configparser
import os
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

import yaml

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

TESTCASE_FILES = ("testcase.yaml", "sample.yaml")

# Directories never descended into (unless explicitly given as a test root).
SKIP_DIRS = frozenset(
    {
        ".git",
        ".west",
        ".lazyzephyr",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "build",
        "zephyr",
        "modules",
        "external",
    }
)
SKIP_PREFIXES = ("twister-out", "build-", "build_", ".")

# Same source extensions twister scans for ztest cases.
SOURCE_SUFFIXES = frozenset({".c", ".cpp", ".cxx", ".cc"})

# ZTEST(suite, test), ZTEST_F, ZTEST_USER, ZTEST_USER_F
_ZTEST_RE = re.compile(
    r"^[ \t]*ZTEST(?:_USER)?(?:_F)?\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)", re.MULTILINE
)


@dataclass(frozen=True)
class ZtestCase:
    """A single ztest test function."""

    suite: str
    func: str
    file: Path
    line: int

    @property
    def name(self) -> str:
        # Mirrors twister: tc_name.replace('test_', '', 1)
        return self.func.replace("test_", "", 1)

    @property
    def id(self) -> str:
        """Identifier as reported in twister.json ("suite.case")."""
        return f"{self.suite}.{self.name}"

    @property
    def filter(self) -> str:
        """Argument for the ztest `-test=` option on native targets."""
        return f"{self.suite}::{self.func}"


@dataclass
class Scenario:
    """One entry of the `tests:` mapping in a testcase.yaml / sample.yaml."""

    name: str
    directory: Path
    yaml_file: Path
    tags: list[str] = field(default_factory=list)
    platform_allow: list[str] = field(default_factory=list)
    integration_platforms: list[str] = field(default_factory=list)
    build_only: bool = False
    harness: str | None = None
    cases: list[ZtestCase] = field(default_factory=list)

    @property
    def suites(self) -> list[str]:
        return sorted({c.suite for c in self.cases})


@dataclass
class Workspace:
    topdir: Path | None
    zephyr_base: Path | None


@dataclass
class Discovery:
    roots: list[Path]
    scenarios: list[Scenario]
    errors: dict[Path, str]

    @cached_property
    def _by_name(self) -> dict[str, Scenario]:
        return {s.name: s for s in self.scenarios}

    def by_name(self) -> dict[str, Scenario]:
        """Scenarios by name (cached: a Discovery's scenarios never change)."""
        return self._by_name

    def under(self, folder: Path) -> list[Scenario]:
        """Scenarios whose directory is `folder` or below it."""
        folder = folder.resolve()
        return [
            s
            for s in self.scenarios
            if s.directory == folder or folder in s.directory.parents
        ]

    def platforms(self) -> list[str]:
        found: set[str] = set()
        for s in self.scenarios:
            found.update(s.platform_allow)
            found.update(s.integration_platforms)
        return sorted(found)


def find_workspace(start: Path) -> Workspace:
    """Walk upwards from `start` looking for a west workspace (.west/config)."""
    start = start.resolve()
    for candidate in (start, *start.parents):
        config_file = candidate / ".west" / "config"
        if config_file.is_file():
            zephyr_base = None
            parser = configparser.ConfigParser()
            try:
                parser.read(config_file)
                base = parser.get("zephyr", "base", fallback=None)
                if base:
                    zephyr_base = (candidate / base).resolve()
            except configparser.Error:
                pass
            if zephyr_base is None and os.environ.get("ZEPHYR_BASE"):
                zephyr_base = Path(os.environ["ZEPHYR_BASE"]).resolve()
            return Workspace(topdir=candidate, zephyr_base=zephyr_base)
    env_base = os.environ.get("ZEPHYR_BASE")
    return Workspace(topdir=None, zephyr_base=Path(env_base).resolve() if env_base else None)


def default_roots(cwd: Path) -> list[Path]:
    """`./tests` if present (matches `west twister -T tests`), else the cwd."""
    tests = cwd / "tests"
    return [tests.resolve()] if tests.is_dir() else [cwd.resolve()]


def _as_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(SKIP_PREFIXES)


def find_ztest_cases(directory: Path) -> list[ZtestCase]:
    """Scan a test directory's sources for ztest cases, like twister does."""
    cases: list[ZtestCase] = []
    seen: set[tuple[str, str]] = set()
    for dirpath, dirnames, filenames in os.walk(directory):
        dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
        for filename in sorted(filenames):
            path = Path(dirpath) / filename
            if path.suffix not in SOURCE_SUFFIXES:
                continue
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for match in _ZTEST_RE.finditer(text):
                key = (match.group(1), match.group(2))
                if key in seen:
                    continue
                seen.add(key)
                line = text.count("\n", 0, match.start()) + 1
                cases.append(ZtestCase(key[0], key[1], path, line))
    return cases


def parse_testcase_yaml(path: Path) -> list[Scenario]:
    """Parse one testcase.yaml / sample.yaml into scenarios.

    Raises ValueError on malformed content.
    """
    try:
        data = yaml.load(path.read_text(), Loader=_Loader)
    except yaml.YAMLError as exc:
        raise ValueError(f"YAML error: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("top level is not a mapping")
    tests = data.get("tests")
    if not isinstance(tests, dict):
        raise ValueError("missing or invalid 'tests:' mapping")
    common = data.get("common") or {}
    if not isinstance(common, dict):
        raise ValueError("'common:' is not a mapping")

    directory = path.parent.resolve()
    cases = find_ztest_cases(directory)
    scenarios = []
    for name, spec in tests.items():
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ValueError(f"scenario {name!r} is not a mapping")

        def merged_list(key: str) -> list[str]:
            items = _as_list(common.get(key)) + _as_list(spec.get(key))
            return list(dict.fromkeys(items))

        def merged(key: str):
            return spec.get(key, common.get(key))

        scenarios.append(
            Scenario(
                name=str(name),
                directory=directory,
                yaml_file=path.resolve(),
                tags=merged_list("tags"),
                platform_allow=merged_list("platform_allow"),
                integration_platforms=merged_list("integration_platforms"),
                build_only=bool(merged("build_only")),
                harness=merged("harness"),
                cases=cases,
            )
        )
    return scenarios


def discover(roots: list[Path]) -> Discovery:
    scenarios: list[Scenario] = []
    errors: dict[Path, str] = {}
    for root in roots:
        root = root.resolve()
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not _skip_dir(d))
            for filename in TESTCASE_FILES:
                if filename in filenames:
                    path = Path(dirpath) / filename
                    try:
                        scenarios.extend(parse_testcase_yaml(path))
                    except (ValueError, OSError) as exc:
                        errors[path] = str(exc)
    scenarios.sort(key=lambda s: (str(s.directory), s.name))
    return Discovery(roots=[r.resolve() for r in roots], scenarios=scenarios, errors=errors)
