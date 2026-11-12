"""A node's settings as a short, readable list: what's set and what's required
first, child devices next, everything else under "more"."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rich.text import Text

from . import values
from .session import READ_ONLY, REF_NODES

# Set by the SoC, or only there to describe other properties: under "more"
# even when set.
_ADVANCED = re.compile(r"^#|^interrupts?(-extended)?$|^interrupt-(parent|names)$|^reg$|"
                       r"-names$|^pinctrl-[2-9]$")

_NAMES = {
    "pinctrl-0": "Pins",
    "pinctrl-1": "Pins when asleep",
    "gpios": "GPIO",
    "current-speed": "Baud rate",
    "reg": "Registers",
    "label": "Label",
}

# Types edited by toggling, picking from a list, or the phandle-array rows.
TOGGLE = {"boolean"}
LABEL_TYPES = {"phandle", "path"}


@dataclass
class Row:
    id: str  # property name, or "status", "more:", "add:", "new:", "node:<path>", "head:<n>"
    kind: str  # status, prop, readonly, entry, more, child, add, new, head
    text: Text
    help: str = ""  # shown under the list while the row is highlighted


def friendly(name: str) -> str:
    """`clock-frequency` -> `Clock frequency`, `nordic,fast-mode` -> `Fast mode`
    (the property's real name is in the help line)."""
    if name in _NAMES:
        return _NAMES[name]
    words = name.rpartition(",")[2].replace("-", " ").replace("_", " ")
    return words[:1].upper() + words[1:]


def hz(v: int) -> str:
    for unit, div in (("GHz", 10**9), ("MHz", 10**6), ("kHz", 10**3)):
        if v >= div and v % (div // 1000 or 1) == 0:
            return f"{v / div:g} {unit}"
    return f"{v} Hz"


def source_text(session, path: str, name: str) -> str | None:
    """The value in effect, as written in the source (keeps macros)."""
    eff = session.effective(path, name)
    for f, stmt in reversed(session.model.prop_sources(path, name)):
        if eff == (f, stmt.line) and stmt.kind == "set":
            return stmt.value
    return None


def show_value(session, node, name: str, spec) -> Text:
    """A property's value for people: `400 kHz  I2C_BITRATE_FAST`, `yes`, `&uart0`."""
    t = Text()
    is_set = name in node._node.props
    type_ = spec.type if spec else None
    if type_ == "boolean":
        t.append("yes" if is_set else "no", style="green" if is_set else "bright_black")
        return t
    if not is_set:
        if spec is not None and spec.required:
            t.append("missing (required)", style="red")
        elif spec is not None and spec.default is not None:
            t.append(f"{_default_text(name, spec.default)} (default)", style="bright_black")
        else:
            t.append("not set", style="bright_black")
        return t
    raw = source_text(session, node.path, name)
    prop = node.props.get(name)
    shown = values.to_input(type_, raw) if raw is not None and type_ else raw
    if shown is not None and type_ == "phandle-array" and shown.count("<") == 1 and \
            shown.startswith("<") and shown.endswith(">"):
        shown = shown[1:-1].strip()  # one entry: no need for the brackets
    val = prop.val if prop is not None else None
    if isinstance(val, int) and not isinstance(val, bool) and _is_hz(name):
        t.append(hz(val))
        if shown and shown.strip() != str(val):
            t.append(f"  {shown}", style="bright_black")
        return t
    if shown is None:
        shown = values.show(val) if prop is not None else "set"
    if type_ in ("string", "string-array") and shown.startswith('"') and shown.count('"') == 2:
        shown = shown[1:-1]
    t.append(shown, style="yellow" if type_ in LABEL_TYPES or type_ == "phandles" else "")
    return t


def _is_hz(name: str) -> bool:
    return name.endswith(("frequency", "-hz", "-freq")) or name in ("clock-frequency",)


def _default_text(name: str, default) -> str:
    if isinstance(default, int) and not isinstance(default, bool) and _is_hz(name):
        return hz(default)
    return str(default)


def _help(spec, name: str, where: str) -> str:
    desc = " ".join((spec.description or "").split()) if spec else ""
    first = re.split(r"(?<=\.)\s", desc, maxsplit=1)[0] if desc else ""
    parts = [p for p in (first, name, where) if p]
    return "  ·  ".join(parts)


def rows(session, node, show_more: bool, base_name=lambda p: p.name) -> list[Row]:
    """The settings list for `node`: first what is set, then the device's own
    settings that aren't (generic and fixed ones are under "more")."""
    if node.path in REF_NODES:
        return _ref_rows(session, node)
    out: list[Row] = []
    binding = node._binding
    width = 22

    def line(label: str, value: Text, note: str = "", label_style: str = "bold") -> Text:
        t = Text()
        t.append(label[:width].ljust(width), style=label_style)
        t.append("  ")
        t.append_text(value)
        if note:
            t.append(f"  {note}", style="bright_black")
        return t

    def where(name: str) -> str:
        eff = session.effective(node.path, name)
        return f"set in {base_name(eff[0])}:{eff[1]}" if eff else ""

    editable = session.editable_props(node)
    generic = session.model.generic_props
    mine = set(session.model.writable_files())
    main, more = [], []
    if "status" in editable:
        on = node.status == "okay"
        v = Text("● yes" if on else "○ no", style="green" if on else "bright_black")
        out.append(Row("status", "status", line("Enabled", v),
                       "Whether Zephyr uses this node (status = \"okay\")  ·  status"))
    for name in editable:
        if name == "status":
            continue
        spec = session.spec(node, name)
        is_set = name in node._node.props
        row = Row(name, "prop", line(friendly(name), show_value(session, node, name, spec)),
                  _help(spec, name, where(name)))
        eff = session.effective(node.path, name)
        own = name not in generic and not _ADVANCED.search(name) and spec.const is None
        if own or (spec.required and not is_set) or (eff and eff[0] in mine):
            main.append((not is_set, len(main), row))
        else:
            more.append((not is_set, name, row))
    for name, prop in node._node.props.items():
        if name in editable or name in READ_ONLY or name == "status":
            continue
        edt_prop = node.props.get(name)
        v = Text(values.show(edt_prop.val) if edt_prop else _raw(prop), style="bright_black")
        more.append((False, name, Row(f"ro:{name}", "readonly",
                                      line(friendly(name), v, "read-only", "bright_black"),
                                      f"Not in the binding; press o to edit the source  ·  "
                                      f"{name}  ·  {where(name)}")))
    out += [m[2] for m in sorted(main, key=lambda m: m[:2])]
    if not binding and node.compats:
        out.append(Row("head:nobinding", "head",
                       Text("No binding for this compatible: settings are read-only.",
                            style="red")))

    kids = list(node.children.values())
    if kids or node.buses or binding:
        title = "Devices on this bus" if node.buses else "Child nodes"
        out.append(Row("head:kids", "head", Text(f"\n{title}", style="bold")))
        for child in kids:
            t = Text("  ")
            on = child.status == "okay"
            t.append("● " if on else "○ ", style="green" if on else "bright_black")
            if child.labels:
                t.append(f"{child.labels[0]}: ", style="yellow")
            t.append(child.name, style="" if on else "bright_black")
            if child.compats:
                t.append(f"  {child.compats[0]}", style="cyan")
            out.append(Row(f"node:{child.path}", "child", t, "enter: go to this node"))
        what = "device" if node.buses else "node"
        out.append(Row("add:", "add", Text(f"  + add {what}…", style="green"),
                       f"Add a {what} under {node.name}"))

    if more:
        more.sort(key=lambda m: (m[0], m[1]))
        arrow = "▾" if show_more else "▸"
        out.append(Row("more:", "more",
                       Text(f"\n{arrow} {len(more)} more settings", style="bright_black"),
                       "enter: show or hide the optional and advanced settings"))
        if show_more:
            out += [m[2] for m in more]
    return out


def _raw(prop) -> str:
    text = str(prop).strip().rstrip(";")
    _, eq, value = text.partition("=")
    return " ".join(value.split()) if eq else "yes"


def _ref_rows(session, node) -> list[Row]:
    """/chosen and /aliases: one row per entry, and a row to add one."""
    out = []
    chosen = node.path == "/chosen"
    for name, prop in node._node.props.items():
        t = Text()
        t.append(name[:30].ljust(30), style="bold")
        t.append("  ")
        try:
            target = prop.to_path()
            t.append(f"&{target.labels[0]}" if target.labels else target.path, style="yellow")
        except Exception:  # DTError: not a reference
            t.append(_raw(prop))
        macro = (f"DT_CHOSEN({re.sub(r'[^a-z0-9]', '_', name)})" if chosen
                 else f"DT_ALIAS({re.sub(r'[^a-z0-9]', '_', name)})")
        out.append(Row(name, "entry", t, f"enter: point it at another node  ·  d: remove  ·  "
                                         f"code reads it with {macro}"))
    what = "chosen entry" if chosen else "alias"
    out.append(Row("new:", "new", Text(f"+ add {what}…", style="green"),
                   "Point a name at a node" + (": Zephyr reads entries such as "
                                               "zephyr,console" if chosen else "")))
    return out


# --------------------------------------------------------------------------
# Choices for the pickers
# --------------------------------------------------------------------------


def label_choices(session, token: str) -> list[tuple[str, Text]]:
    """Every labelled node, devices before pinctrl groups and other plain nodes."""
    labels = session.model.edt._dt.label2node
    want = token.lstrip("&").lower()
    if want and want in {k.lower() for k in labels}:
        want = ""  # a complete label: offer all of them, to change it
    out = []
    for label in sorted(labels, key=str.lower):
        low = label.lower()
        if want and want not in low:
            continue
        node = session.node(labels[label].path)
        t = Text()
        t.append(f"&{label}", style="yellow")
        if node is not None and node.compats:
            t.append(f"  {node.compats[0]}", style="cyan")
        t.append(f"  {labels[label].path}", style="bright_black")
        if node is not None and node.status != "okay":
            t.append("  disabled", style="bright_black italic")
        has_compat = node is not None and bool(node.compats)
        out.append((not low.startswith(want), not has_compat, f"&{label}", t))
    out.sort(key=lambda c: c[:2])
    return [(ins, t) for *_, ins, t in out]


def chosen_choices(names: set[str], used: set[str], token: str) -> list[tuple[str, Text]]:
    want = token.lower()
    out = []
    for name in sorted(names | used):
        if want and want not in name:
            continue
        t = Text(name, style="bright_black" if name in used else "bold")
        if name in used:
            t.append("  already set", style="bright_black italic")
        out.append((name in used, not name.startswith(want), name, t))
    out.sort(key=lambda c: c[:2])
    return [(name, t) for *_, name, t in out]


# --------------------------------------------------------------------------
# The reference card for one property
# --------------------------------------------------------------------------


def example_value(session, node, name: str, spec) -> str | None:
    """A DTS right-hand side to start from: the current value, else something
    the binding allows. None for booleans (`name;`)."""
    from . import specifiers
    raw = source_text(session, node.path, name)
    if raw is not None:
        return raw
    type_ = spec.type if spec else None
    if type_ == "boolean":
        return None
    first = spec.enum[0] if spec is not None and spec.enum else None
    default = spec.default if spec is not None else None
    pick = first if first is not None else default
    if type_ in ("string", "string-array"):
        return f'"{pick}"' if pick is not None else '"..."'
    if type_ == "int":
        return f"<{pick}>" if pick is not None else "<0>"
    if type_ == "array":
        return "<" + " ".join(map(str, default)) + ">" if isinstance(default, list) else "<0>"
    if type_ in ("phandle", "phandles"):
        return "<&node>"
    if type_ == "path":
        return "&node"
    if type_ == "phandle-array":
        ctrls = specifiers.controllers(session.model.edt, specifiers.space(name, spec))
        if ctrls:
            c = ctrls[0]
            return "<" + " ".join([c.ref, *(cell.upper().replace(" ", "_")
                                             for cell in c.cells)]) + ">"
        return "<&controller ...>"
    if type_ == "uint8-array":
        return "[00]"
    return "..."


def example(session, node, name: str, spec) -> str:
    """An overlay snippet that sets the property on this node."""
    from .dtsedit import node_ref
    value = example_value(session, node, name, spec)
    line = f"{name};" if value is None else f"{name} = {value};"
    return f"{node_ref(node.path, list(node.labels))} {{\n\t{line}\n}};"


def prop_info(session, node, name: str, base_name=lambda p: p.name) -> Text:
    """Everything worth knowing about one property, for writing it by hand."""
    from . import macros, specifiers
    spec = session.spec(node, name)
    t = Text()
    t.append(name, style="bold")
    if spec is None:
        t.append("\n\nNot in the node's binding: Zephyr won't check or use it.\n",
                 style="bright_black")
        prop = node._node.props.get(name)
        if prop is not None:
            t.append(f"\nValue: {_raw(prop)}\n")
        return t
    facts = [spec.type]
    if spec.required:
        facts.append("required")
    if spec.deprecated:
        facts.append("deprecated")
    t.append("\n" + " · ".join(facts), style="bright_black")
    desc = (spec.description or "").strip()
    if desc:
        t.append("\n\n" + desc + "\n")
    t.append("\nValue: ", style="bold")
    t.append_text(show_value(session, node, name, spec))
    t.append("\n")
    sources = session.model.prop_sources(node.path, name)
    if sources:
        eff = session.effective(node.path, name)
        t.append("Set in\n", style="bold")
        for f, stmt in sources:
            mark = "→ " if eff == (f, stmt.line) else "  "
            what = "/delete-property/" if stmt.kind == "delete-property" else \
                f"= {stmt.value}" if stmt.value is not None else "(set)"
            t.append(f"  {mark}{base_name(f)}:{stmt.line}  ", style="bright_black")
            t.append(what + "\n")
    if spec.enum:
        t.append("\nAllowed values\n", style="bold")
        for v in spec.enum:
            t.append(f"  {v}", style="green" if v == spec.default else "")
            if v == spec.default:
                t.append("  (default)", style="bright_black")
            t.append("\n")
    elif spec.default is not None:
        t.append("\nDefault: ", style="bold")
        t.append(_default_text(name, spec.default) + "\n")
    if spec.const is not None:
        t.append("\nMust be: ", style="bold")
        t.append(f"{spec.const}\n")

    if spec.type == "phandle-array":
        space = specifiers.space(name, spec)
        ctrls = specifiers.controllers(session.model.edt, space)
        t.append("\nFormat\n", style="bold")
        if ctrls:
            shapes: dict[tuple, list[str]] = {}
            for c in ctrls:
                shapes.setdefault(tuple(c.cells), []).append(c.ref)
            for cells, refs in shapes.items():
                t.append(f"  <&controller {' '.join(cells)}>", style="yellow")
                shown = ", ".join(refs[:8]) + (", …" if len(refs) > 8 else "")
                t.append(f"  for {shown}\n", style="bright_black")
            pins = [c for c in ctrls if c.limit]
            if pins:
                t.append(f"  pins: 0–{pins[0].limit - 1} on {pins[0].ref}\n",
                         style="bright_black")
        else:
            t.append(f"  no node in this tree has #{space}-cells\n", style="bright_black")
    elif spec.type in ("phandle", "phandles", "path"):
        t.append("\nPoints at a node, written as ", style="bold")
        t.append("&label" if spec.type == "path" else "<&label>", style="yellow")
        t.append("\n")

    if spec.type in ("int", "array", "phandle-array"):
        context = macros.context_words(*node.compats[:1])
        primary = macros.context_words(name, *node.buses, *node.on_buses,
                                       specifiers.space(name, spec)
                                       if spec.type == "phandle-array" else None)
        found = macros.rank(session.model.macros, "", context | primary, limit=16,
                            primary=primary)
        if _is_hz(name):
            found = [m for m in found if (macros.value_of(m) or 0) >= 1000]
        if found:
            t.append("\nMacros you can use\n", style="bold")
            for m in found:
                t.append(f"  {m.name}{m.args or ''}", style="cyan")
                v = macros.value_of(m)
                shown = hz(v) if v is not None and _is_hz(name) else m.value[:40]
                t.append(f"  {shown}", style="bright_black")
                t.append(f"  {m.header.parent.name}/{m.header.name}\n",
                         style="bright_black italic")

    t.append("\nIn an overlay\n", style="bold")
    for line in example(session, node, name, spec).splitlines():
        t.append(f"  {line}\n", style="yellow")
    return t
