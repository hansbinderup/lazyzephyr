"""Editing session: the parsed tree for one target, and checked edits to its sources.

Every edit is written, the devicetree re-parsed, and the file restored if the
result doesn't parse. The UI only talks to this class, so it is easy to test.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import dtsedit, values
from .model import DtError, Model, load
from .workspace import DtInputs, Workspace

# Properties that every node gets from the base binding and that the editor
# writes itself (compatible on add) or that make no sense to edit by hand.
READ_ONLY = {"compatible", "phandle"}


@dataclass
class EditResult:
    ok: bool
    message: str
    file: Path | None = None
    line: int | None = None


class Session:
    def __init__(self, ws: Workspace, inputs: DtInputs, work_dir: Path | None = None):
        self.ws = ws
        self.inputs = inputs
        self.work_dir = work_dir or Path(tempfile.mkdtemp(prefix="lazydts-"))
        self.model: Model | None = None
        self.error: str | None = None
        self.undo_stack: list[list[tuple[Path, str | None]]] = []
        self._mtimes: dict[Path, float] = {}

    # -- loading ---------------------------------------------------------

    def reload(self) -> bool:
        """Parse again. On failure keep the old model and set `error`."""
        try:
            self.model = load(self.ws, self.inputs, self.work_dir)
            self.error = None
        except DtError as exc:
            self.error = str(exc)
            return False
        self._mtimes = self._snapshot()
        return True

    def _watched(self) -> list[Path]:
        files = list(self.inputs.files)
        if self.model:
            files += [p for p in self.model.deps if p not in files]
        return files

    def _snapshot(self) -> dict[Path, float]:
        out = {}
        for f in self._watched():
            try:
                out[f] = f.stat().st_mtime
            except OSError:
                out[f] = -1
        return out

    def changed_on_disk(self) -> bool:
        return self._snapshot() != self._mtimes

    # -- queries ---------------------------------------------------------

    def node(self, path: str):
        try:
            return self.model.edt.get_node(path) if self.model else None
        except Exception:  # EDTError
            return None

    def spec(self, node, name: str):
        binding = getattr(node, "_binding", None)
        return binding.prop2specs.get(name) if binding else None

    def editable_props(self, node) -> list[str]:
        """Properties the binding allows, in binding order."""
        binding = getattr(node, "_binding", None)
        if not binding:
            return []
        return [n for n, s in binding.prop2specs.items()
                if n not in READ_ONLY and s.type in values.EDITABLE]

    def write_files(self, node_path: str | None = None) -> list[Path]:
        """Files an edit of `node_path` may go to: writable inputs at or after the
        input that defines the node (a `&label` must come after the label)."""
        m = self.model
        if not m:
            return []
        files = m.writable_files()
        node = self.node(node_path) if node_path else None
        if node is None:
            return files
        first = Path(node._node.filename).resolve()
        idx = 0
        for i, f in enumerate(self.inputs.files):
            if f == first:
                idx = i
        return [f for f in files if self.inputs.files.index(f) >= idx]

    def default_write_file(self, node_path: str | None = None,
                           prop: str | None = None) -> Path | None:
        """Where an edit goes unless the user says otherwise: the writable file
        that already sets the property, else the one that already touches the
        node, else the app overlay."""
        files = self.write_files(node_path)
        if not files:
            return None
        if node_path and prop:
            hits = [f for f, _ in self.model.prop_sources(node_path, prop) if f in files]
            if hits:
                return hits[-1]
        if node_path:
            hits = [f for f, _ in self.model.node_sources(node_path) if f in files]
            user = [f for f in hits if f in self.inputs.user_files]
            if user:
                return user[-1]
        default = self.model.default_write_file()
        return default if default in files else files[-1]

    def effective(self, node_path: str, name: str) -> tuple[Path, int] | None:
        node = self.node(node_path)
        prop = node._node.props.get(name) if node else None
        if not prop:
            return None
        return Path(prop.filename).resolve(), prop.lineno

    # -- editing ---------------------------------------------------------

    def _apply(self, changes: dict[Path, str], what: str) -> EditResult:
        """Write `changes`, re-parse, and roll back if it fails."""
        old: list[tuple[Path, str | None]] = []
        for path, text in changes.items():
            old.append((path, path.read_text() if path.exists() else None))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if self.reload():
            self.undo_stack.append(old)
            return EditResult(True, what)
        error = self.error
        self._restore(old)
        self.reload()
        return EditResult(False, error or "devicetree does not parse")

    @staticmethod
    def _restore(old: list[tuple[Path, str | None]]) -> None:
        for path, text in old:
            if text is None:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            else:
                path.write_text(text)

    def undo(self) -> EditResult:
        if not self.undo_stack:
            return EditResult(False, "nothing to undo")
        self._restore(self.undo_stack.pop())
        self.reload()
        return EditResult(True, "undone")

    def _source(self, file: Path) -> dtsedit.SourceFile:
        sf = self.model.source(file) if file.exists() else dtsedit.SourceFile("")
        if sf is None:
            raise DtError(f"can't make sense of {file.name}; edit it by hand")
        return sf

    def _check_file(self, file: Path, node_path: str | None) -> str | None:
        if file not in self.write_files(node_path):
            return f"{file.name} can't be edited here"
        return None

    def set_property(self, node_path: str, name: str, text: str | None, file: Path,
                     delete: bool = False) -> EditResult:
        """Set property `name` from user input `text` (None: boolean true).

        delete=True removes it: drops the assignment from `file` if that is the
        one in effect, else adds /delete-property/.
        """
        node = self.node(node_path)
        if node is None:
            return EditResult(False, f"no node {node_path}")
        spec = self.spec(node, name)
        if spec is None or name not in self.editable_props(node):
            return EditResult(False, f"{name} is not a property of this node's binding")
        if err := self._check_file(file, node_path):
            return EditResult(False, err)
        try:
            sf = self._source(file)
        except DtError as exc:
            return EditResult(False, str(exc))
        labels = list(node.labels)
        if delete:
            here = [s for s in sf.props_for(node_path, name) if s.kind == "set"]
            eff = self.effective(node_path, name)
            if here and eff == (file, here[-1].line):
                new = dtsedit.remove_statement(sf, here[-1])
                what = f"removed {name} from {file.name}"
            elif eff is None:
                return EditResult(False, f"{name} is not set")
            else:
                new = dtsedit.delete_property(sf, node_path, labels, name)
                what = f"deleted {name} in {file.name}"
        else:
            if spec.type == "boolean":
                value = None
            else:
                try:
                    value = values.format_value(spec.type, text or "", spec.enum,
                                                self.model.labels)
                except ValueError as exc:
                    return EditResult(False, str(exc))
            new = dtsedit.set_property(sf, node_path, labels, name, value)
            what = f"set {name} in {file.name}"
        result = self._apply({file: new}, what)
        if result.ok:
            result = self._check_effect(result, node_path, name, file, delete)
        return result

    def _check_effect(self, result: EditResult, node_path: str, name: str, file: Path,
                      delete: bool) -> EditResult:
        eff = self.effective(node_path, name)
        if delete:
            if eff:
                result.message += f"; still set by {eff[0].name}:{eff[1]}"
            return result
        if eff and eff[0] != file:
            result.message += f" (but {eff[0].name}:{eff[1]} overrides it)"
        elif eff:
            result.file, result.line = eff
        return result

    def toggle_status(self, node_path: str, file: Path) -> EditResult:
        node = self.node(node_path)
        if node is None:
            return EditResult(False, f"no node {node_path}")
        new = "disabled" if node.status == "okay" else "okay"
        return self.set_property(node_path, "status", new, file)

    def add_node(self, parent_path: str, name: str, label: str | None,
                 props: list[tuple[str, str | None]], file: Path) -> EditResult:
        """Add node `name` under `parent_path`. `props` hold DTS values, already
        formatted (see values.format_value)."""
        parent = self.node(parent_path)
        if parent is None:
            return EditResult(False, f"no node {parent_path}")
        if not dtsedit.NODE_NAME.match(name):
            return EditResult(False, f"invalid node name: {name}")
        if name in parent.children:
            return EditResult(False, f"{parent_path} already has a child {name}")
        if label:
            if not dtsedit.LABEL_NAME.match(label):
                return EditResult(False, f"invalid label: {label}")
            if label in self.model.labels:
                return EditResult(False, f"label {label} is already used")
        if err := self._check_file(file, parent_path):
            return EditResult(False, err)
        try:
            sf = self._source(file)
        except DtError as exc:
            return EditResult(False, str(exc))
        lines = dtsedit.node_lines(name, label, props)
        new = dtsedit.add_node(sf, parent_path, list(parent.labels), lines)
        path = ("/" if parent_path == "/" else parent_path + "/") + name
        result = self._apply({file: new}, f"added {path} to {file.name}")
        if result.ok:
            for f, block in self.model.node_sources(path):
                if f == file:
                    result.file, result.line = f, block.line
        return result

    def delete_plan(self, node_path: str, file: Path) -> tuple[str, str] | None:
        """(description, kind) of what deleting the node will do, or None."""
        node = self.node(node_path)
        if node is None or node_path == "/":
            return None
        first = Path(node._node.filename).resolve()
        if first == file:
            return (f"Remove the node's definition (and its other blocks) from {file.name}",
                    "remove")
        ref = node_ref_text(node_path, list(node.labels))
        return (f"Add /delete-node/ {ref}; to {file.name}", "delete-node")

    def delete_node(self, node_path: str, file: Path) -> EditResult:
        plan = self.delete_plan(node_path, file)
        if plan is None:
            return EditResult(False, "can't delete this node")
        if err := self._check_file(file, node_path):
            return EditResult(False, err)
        node = self.node(node_path)
        sf = self._source(file)
        if plan[1] == "remove":
            prefix = node_path + "/"
            text = sf.text
            for block in sorted((b for b in sf.blocks
                                 if b.path == node_path or (b.path or "").startswith(prefix)),
                                key=lambda b: b.start, reverse=True):
                if any(o is not block and o.start < block.start and o.end >= block.end
                       and (o.path == node_path or (o.path or "").startswith(prefix))
                       for o in sf.blocks):
                    continue  # inside a block that goes anyway
                text = dtsedit.remove_block(dtsedit.SourceFile(text, [block]), block)
            new = text
        else:
            new = dtsedit.delete_node_stmt(sf, node_path, list(node.labels))
        return self._apply({file: new}, f"deleted {node_path}")


def node_ref_text(path: str, labels: list[str]) -> str:
    return f"&{labels[0]}" if labels else f"&{{{path}}}"
