"""Preprocess and parse a target's devicetree with Zephyr's own edtlib."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import checks, dtsedit
from . import macros as dtmacros
from .workspace import DtInputs, Workspace


class DtError(Exception):
    """The devicetree doesn't preprocess or parse."""


def import_edtlib(zephyr_base: Path):
    src = str(zephyr_base / "scripts" / "dts" / "python-devicetree" / "src")
    if src not in sys.path:
        sys.path.insert(0, src)
    return importlib.import_module("devicetree.edtlib")


def pick_compiler(preferred: str | None) -> str:
    for cc in (preferred, "gcc", "cc", "clang"):
        if cc and (Path(cc).is_file() or shutil.which(cc)):
            return cc
    raise DtError("no C compiler found to preprocess the devicetree (tried gcc, cc, clang)")


def preprocess(inputs: DtInputs, zephyr_base: Path, out_dir: Path) -> tuple[Path, list[Path]]:
    """Run the C preprocessor like zephyr_dt_preprocess(). Returns (output, dependencies)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out, deps = out_dir / "zephyr.dts.pre", out_dir / "zephyr.dts.d"
    missing = [f for f in inputs.files if not f.is_file()]
    if missing:
        raise DtError(f"missing devicetree file: {missing[0]}")
    cmd = [pick_compiler(inputs.compiler), "-x", "assembler-with-cpp", "-nostdinc"]
    for d in inputs.include_dirs:
        cmd += ["-isystem", str(d)]
    for f in inputs.files:
        cmd += ["-include", str(f)]
    cmd += ["-undef", "-D__DTS__", "-E", "-MD", "-MF", str(deps), "-o", str(out),
            str(zephyr_base / "misc" / "empty_file.c")]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          cwd=inputs.workdir if inputs.workdir and inputs.workdir.is_dir()
                          else None)
    if proc.returncode:
        raise DtError(proc.stderr.strip() or f"preprocessor failed ({proc.returncode})")
    return out, _read_deps(deps)


def _read_deps(path: Path) -> list[Path]:
    try:
        text = path.read_text().replace("\\\n", " ")
    except OSError:
        return []
    _, _, files = text.partition(":")
    out = []
    for name in files.split():
        p = Path(name).resolve()
        if p.suffix != ".c" and p not in out:
            out.append(p)
    return out


@dataclass
class Model:
    """A parsed devicetree and the source files it came from."""

    ws: Workspace
    inputs: DtInputs
    edt: object
    deps: list[Path]
    _sources: dict[Path, dtsedit.SourceFile | None] = field(default_factory=dict)
    # Files #included inside a node -> that node's path.
    _roots: dict[Path, str] | None = None
    _macros: list[dtmacros.Macro] | None = None
    _references: dict[str, list[checks.Ref]] | None = None
    _problems: list[checks.Problem] | None = None

    @property
    def labels(self) -> set[str]:
        return set(self.edt._dt.label2node)

    def label_path(self, label: str) -> str | None:
        node = self.edt._dt.label2node.get(label)
        return node.path if node else None

    def child_path(self, parent: str, name: str, labels: list[str]) -> str | None:
        """Path of child `name` of `parent`, also when the name uses macros."""
        for label in labels:
            if path := self.label_path(label):
                return path
        path = "/" + name if parent == "/" else f"{parent}/{name}"
        try:
            node = self.edt.get_node(parent)
        except Exception:  # EDTError: parent not in the tree (deleted)
            return path
        if name in node.children:
            return path
        base = name.split("@")[0]
        matches = [c for c in node.children.values()
                   if c.name.split("@")[0] == base]
        return matches[0].path if len(matches) == 1 else path

    @property
    def macros(self) -> list[dtmacros.Macro]:
        """Macros from the dt-bindings headers this tree includes."""
        if self._macros is None:
            self._macros = dtmacros.scan([p for p in self.deps if dtmacros.is_binding_header(p)])
        return self._macros

    @property
    def references(self) -> dict[str, list[checks.Ref]]:
        """Node path -> the properties that point at it."""
        if self._references is None:
            self._references = checks.references(self.edt)
        return self._references

    @property
    def problems(self) -> list[checks.Problem]:
        if self._problems is None:
            self._problems = checks.problems(self.edt, self.macros, self.references)
        return self._problems

    def dts_files(self) -> list[Path]:
        """Devicetree sources (not headers) in include order."""
        return [p for p in self.deps if p.suffix in (".dts", ".dtsi", ".overlay")]

    def source(self, path: Path) -> dtsedit.SourceFile | None:
        path = Path(path).resolve()
        if self._roots is None:
            self._find_roots()
        if path not in self._sources:
            self._scan(path, self._roots.get(path))
        return self._sources.get(path)

    def _find_roots(self) -> None:
        """Find files #included inside a node, also through other includes."""
        self._roots = {}
        todo = list(self.dts_files())
        while todo:
            f = todo.pop(0)
            sf = self._scan(f, self._roots.get(f))
            for name, node in sf.nested_includes if sf else ():
                name = name.split("/")[-1]
                for dep in self.dts_files():
                    if dep.name == name and self._roots.get(dep) != node:
                        self._roots[dep] = node
                        self._sources.pop(dep, None)
                        todo.append(dep)

    def _scan(self, path: Path, root: str | None) -> dtsedit.SourceFile | None:
        if path in self._sources:
            return self._sources[path]
        try:
            sf = dtsedit.scan(path.read_text(), self.label_path, self.child_path, root)
        except (OSError, UnicodeDecodeError, dtsedit.ScanError):
            sf = None
        self._sources[path] = sf
        return sf

    def node_sources(self, path: str) -> list[tuple[Path, dtsedit.Block]]:
        """Every block in the sources that defines or overrides node `path`."""
        out = []
        for f in self.dts_files():
            sf = self.source(f)
            if sf:
                out += [(f, b) for b in sf.blocks_for(path)]
        return out

    def prop_sources(self, path: str, name: str) -> list[tuple[Path, dtsedit.Statement]]:
        """Every assignment/deletion of property `name` on node `path`, in order."""
        out = []
        for f in self.dts_files():
            sf = self.source(f)
            if sf:
                out += [(f, s) for s in sf.props_for(path, name)]
        return out

    def writable_files(self) -> list[Path]:
        """Top-level inputs the user may edit, in include order (last wins)."""
        return [f for f in self.inputs.files if not self.ws.is_upstream(f)]

    def default_write_file(self) -> Path | None:
        user = [f for f in self.inputs.user_files if not self.ws.is_upstream(f)]
        files = user or self.writable_files()
        return files[-1] if files else None


def load(ws: Workspace, inputs: DtInputs, out_dir: Path) -> Model:
    edtlib = import_edtlib(ws.zephyr_base)
    pre, deps = preprocess(inputs, ws.zephyr_base, out_dir)
    try:
        edt = edtlib.EDT(str(pre), [str(d) for d in inputs.bindings_dirs],
                         warn_reg_unit_address_mismatch=False)
    except Exception as exc:  # EDTError, DTError, yaml errors
        raise DtError(str(exc)) from None
    return Model(ws, inputs, edt, deps)
