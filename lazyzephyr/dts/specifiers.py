"""phandle-array values (`gpios`, `pwms`, `io-channels`…) as entries of a controller
and named cells, so they can be edited one field at a time."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import values

_SIMPLE_CELL = re.compile(r"^\w+$|^\w+\(.*\)$")


@dataclass
class Controller:
    ref: str  # "&gpio0", or "&{/path}" for a node without a label
    path: str
    cells: list[str]
    compat: str = ""
    okay: bool = True
    limit: int | None = None  # number of pins, for gpio controllers
    refs: set[str] = field(default_factory=set)  # every way to refer to it

    @property
    def title(self) -> str:
        parts = [self.ref]
        if self.compat:
            parts.append(self.compat)
        if not self.okay:
            parts.append("disabled")
        return "  ".join(parts)


Entry = tuple[str, list[str]]


def space(name: str, spec) -> str:
    """The specifier space of property `name`, as edtlib works it out."""
    explicit = getattr(spec, "specifier_space", None)
    if explicit:
        return explicit
    return "gpio" if name.endswith("gpios") else name[:-1]


def controllers(edt, space_: str) -> list[Controller]:
    """Nodes that can be the controller in a `space_` phandle-array, enabled first."""
    key = f"#{space_}-cells"
    out = []
    for n in edt.nodes:
        prop = n._node.props.get(key)
        if prop is None:
            continue
        try:
            count = prop.to_num()
        except Exception:  # DTError: not a single cell
            continue
        names = list(n._binding.specifier2cells.get(space_, [])) if n._binding else []
        names = (names + [f"cell {i + 1}" for i in range(len(names), count)])[:count]
        ref = f"&{n.labels[0]}" if n.labels else f"&{{{n.path}}}"
        limit = None
        if space_ == "gpio" and "ngpios" in n.props:
            limit = n.props["ngpios"].val
        refs = {f"&{label}" for label in n.labels} | {f"&{{{n.path}}}"}
        out.append(Controller(ref, n.path, names, n.compats[0] if n.compats else "",
                              n.status == "okay", limit, refs))
    # Nexus nodes (e.g. an Arduino header's gpio-map) rarely name their cells:
    # borrow the names from a controller with as many cells.
    def unnamed(c: Controller) -> bool:
        return bool(c.cells) and c.cells[0].startswith("cell ")
    named = {len(c.cells): c.cells for c in out if c.cells and not unnamed(c)}
    for c in out:
        if unnamed(c) and len(c.cells) in named:
            c.cells = list(named[len(c.cells)])
    return sorted(out, key=lambda c: (not c.okay, c.ref.startswith("&{"), c.ref))


def parse(text: str | None, ctrls: list[Controller]) -> list[Entry] | None:
    """Entries in `text` as written in the source, or None if it can't be split
    into known controllers and their cells (then it is edited as text)."""
    if text is None or not text.strip():
        return []
    by_ref = {r: c for c in ctrls for r in c.refs}
    if re.sub(r"<[^>]*>", "", text).strip(" \t\n,"):
        return None  # something besides <...> groups, e.g. a macro
    out: list[Entry] = []
    for group in re.findall(r"<([^>]*)>", text):
        try:
            cells = values._cells(group)
        except ValueError:
            return None
        while cells:
            ctrl = by_ref.get(cells[0])
            n = len(ctrl.cells) if ctrl else 0
            if ctrl is None or len(cells) < n + 1:
                return None
            out.append((ctrl.ref, cells[1:n + 1]))
            cells = cells[n + 1:]
    return out


def cell_text(cell: str) -> str:
    """A cell as DTS needs it: expressions such as `A | B` go in parentheses."""
    cell = cell.strip()
    if _SIMPLE_CELL.match(cell):
        return cell
    if cell.startswith("(") and cell.endswith(")"):
        try:
            if len(values._cells(cell)) == 1:
                return cell
        except ValueError:
            pass
    return f"({cell})"


def compose(entries: list[Entry]) -> str:
    return ", ".join("<" + " ".join([ref, *map(cell_text, cells)]) + ">"
                     for ref, cells in entries)


def check(entries: list[Entry], ctrls: list[Controller]) -> str | None:
    """Why `entries` can't be written, or None."""
    by_ref = {c.ref: c for c in ctrls}
    if not entries:
        return "add at least one entry (ctrl+n), or remove the property instead"
    for i, (ref, cells) in enumerate(entries, 1):
        where = f"entry {i}: " if len(entries) > 1 else ""
        ctrl = by_ref.get(ref)
        if ctrl is None:
            return f"{where}pick a controller"
        for name, cell in zip(ctrl.cells, cells):
            if not cell.strip():
                return f"{where}{name} is empty"
            if name == "pin" and ctrl.limit is not None and re.fullmatch(r"\d+", cell.strip()):
                if int(cell) >= ctrl.limit:
                    return f"{where}pin {cell} is out of range: {ctrl.ref} has {ctrl.limit} pins"
    return None
