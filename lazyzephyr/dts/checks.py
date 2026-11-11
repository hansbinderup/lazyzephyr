"""What the tree says beyond its properties: which nodes point at a node, and
problems that parse fine but won't work (two users of one pin, two devices at
one address, overlapping partitions, enabled nodes that need disabled ones…)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# dtlib marker types (dtlib._MarkerType): &foo in a string, <&foo> in cells.
_MARKER_PATH, _MARKER_PHANDLE = 0, 1


@dataclass
class Ref:
    """Property `prop` of node `source` points at a node."""
    source: str
    prop: str
    detail: str = ""  # the cells after the phandle, e.g. "pin=13 flags=1"


@dataclass
class Problem:
    severity: str  # "error" or "warning"
    message: str
    paths: list[str]  # the nodes involved, the first is where to look
    prop: str | None = None
    kind: str = ""

    @property
    def path(self) -> str:
        return self.paths[0]


# --------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------


def _cell(value: bytes, i: int) -> int | None:
    chunk = value[i:i + 4]
    return int.from_bytes(chunk, "big") if len(chunk) == 4 else None


def _space(prop_name: str, edt_prop) -> str | None:
    spec = getattr(edt_prop, "spec", None)
    if spec is None or spec.type != "phandle-array":
        return None
    if spec.specifier_space:
        return spec.specifier_space
    return "gpio" if prop_name.endswith("gpios") else prop_name[:-1]


def references(edt) -> dict[str, list[Ref]]:
    """Target node path -> every reference to it, as written in the sources
    (an entry through a nexus like an Arduino header points at the header)."""
    dt = edt._dt
    out: dict[str, list[Ref]] = {}
    # Cell names by (space, count), for controllers whose binding names none
    # (nexus nodes such as an Arduino header).
    known: dict[tuple[str, int], list[str]] = {}
    for n in edt.nodes:
        for sp, names in (n._binding.specifier2cells.items() if n._binding else ()):
            if names:
                known.setdefault((sp, len(names)), list(names))
    for node in dt.node_iter():
        try:
            enode = edt.get_node(node.path)
        except Exception:  # EDTError
            enode = None
        for name, prop in node.props.items():
            markers = [m for m in prop._markers if m[1] in (_MARKER_PATH, _MARKER_PHANDLE)]
            if not markers:
                continue
            space = _space(name, enode.props.get(name)) if enode else None
            for pos, kind, ref in markers:
                try:
                    target = dt._ref2node(ref)
                except Exception:  # DTError: dangling (can't happen once parsed)
                    continue
                detail = ""
                if kind == _MARKER_PHANDLE and space:
                    detail = _specifier_detail(edt, target, space, prop.value, pos, known)
                out.setdefault(target.path, []).append(Ref(node.path, name, detail))
    return out


def _specifier_detail(edt, target, space: str, value: bytes, pos: int,
                      known: dict[tuple[str, int], list[str]]) -> str:
    cells_prop = target.props.get(f"#{space}-cells")
    try:
        count = cells_prop.to_num() if cells_prop else 0
    except Exception:  # DTError
        return ""
    try:
        binding = edt.get_node(target.path)._binding
    except Exception:  # EDTError
        binding = None
    names = list(binding.specifier2cells.get(space, [])) if binding else []
    if not names:
        names = known.get((space, count), [])
    parts = []
    for i in range(count):
        v = _cell(value, pos + 4 + 4 * i)
        if v is None:
            break
        name = names[i] if i < len(names) else f"cell{i + 1}"
        parts.append(f"{name}={v}")
    return " ".join(parts)


# --------------------------------------------------------------------------
# Problems
# --------------------------------------------------------------------------


def enabled(node) -> bool:
    """okay, and so is every parent (a child of a disabled node is off too)."""
    while node is not None:
        if node.status != "okay":
            return False
        node = node.parent
    return True


def _name(node) -> str:
    return f"&{node.labels[0]}" if node.labels else node.path


def _int(text: str) -> int | None:
    try:
        return int(text.strip().rstrip("uUlL"), 0)
    except ValueError:
        return None


@dataclass
class PinDecoder:
    """Turns a vendor's pinctrl cell into a pin name."""
    prop: str
    decode: object  # Callable[[int], str | None]


def pin_decoders(macros) -> list[PinDecoder]:
    """Decoders for the pinctrl encodings this tree includes (see the dt-bindings
    pinctrl headers): nRF psels, STM32 pinmux and Ambiq Apollo pinmux."""
    values = {m.name: m.value for m in macros}
    out = []
    if "NRF_PIN_MSK" in values:
        mask = _int(values["NRF_PIN_MSK"]) or 0x1FF

        def nrf(v: int) -> str | None:
            pin = v & mask
            return None if pin == mask else f"P{pin // 32}.{pin % 32:02d}"
        out.append(PinDecoder("psels", nrf))
    if "STM32_PORT_SHIFT" in values:
        port_shift = _int(values["STM32_PORT_SHIFT"]) or 9
        line_shift = _int(values.get("STM32_LINE_SHIFT", "5")) or 5
        out.append(PinDecoder("pinmux", lambda v: "P" + chr(ord("A") + ((v >> port_shift) & 0x1F))
                              + str((v >> line_shift) & 0xF)))
    for name, value in values.items():
        m = re.fullmatch(r"(APOLLO\d+)_PIN_NUM_POS", name)
        if m and f"{m.group(1)}_PIN_NUM_MASK" in values:
            pos, mask = _int(value), _int(values[f"{m.group(1)}_PIN_NUM_MASK"])
            if pos is not None and mask is not None:
                out.append(PinDecoder("pinmux", lambda v, p=pos, k=mask: f"P{(v >> p) & k}"))
                break
    return out


def _gpio_pin(ctrl, pin: int) -> str:
    """A GPIO pin's name, the same as pinctrl's where the vendor makes that easy."""
    port = ctrl.props.get("port")
    if port is not None and any(c.startswith("nordic,") for c in ctrl.compats):
        return f"P{port.val}.{pin:02d}"
    # Ambiq: banks of 32 pins named after their range, e.g. gpio64_95.
    bank = re.fullmatch(r"gpio(\d+)_\d+", ctrl.name.split("@")[0])
    if bank and "ambiq,gpio-bank" in ctrl.compats:
        return f"P{int(bank.group(1)) + pin}"
    return f"{_name(ctrl)} pin {pin}"


def _pin_users(edt, decoders: list[PinDecoder]) -> dict[str, list[tuple[object, str]]]:
    """Pin name -> (node, how it uses it) for every enabled node."""
    users: dict[str, list[tuple[object, str]]] = {}

    def add(pin: str, node, how: str) -> None:
        lst = users.setdefault(pin, [])
        if all(n is not node for n, _ in lst):
            lst.append((node, how))

    for node in edt.nodes:
        if not enabled(node):
            continue
        for name, prop in node.props.items():
            if _space(name, prop) != "gpio":
                continue
            for entry in prop.val or []:
                pin = (entry.data or {}).get("pin") if entry is not None else None
                if pin is not None:
                    add(_gpio_pin(entry.controller, pin), node, name)
        if not decoders:
            continue
        pinctrls = node.pinctrls
        state = next((p for p in pinctrls if p.name == "default"),
                     pinctrls[0] if pinctrls else None)
        for conf in state.conf_nodes if state else []:
            for part in [conf, *conf.children.values()]:
                for dec in decoders:
                    prop = part._node.props.get(dec.prop)
                    if prop is None:
                        continue
                    try:
                        cells = prop.to_nums()
                    except Exception:  # DTError
                        continue
                    for v in cells:
                        pin = dec.decode(v)
                        if pin:
                            add(pin, node, f"pinctrl-0 ({_name(conf)})")
    return users


def problems(edt, macros=(), refs: dict[str, list[Ref]] | None = None) -> list[Problem]:
    """Everything that looks wrong, errors first."""
    out: list[Problem] = []

    # Two enabled nodes on one pin.
    for pin, users in sorted(_pin_users(edt, pin_decoders(macros)).items()):
        if len(users) > 1:
            who = ", ".join(f"{_name(n)} ({how})" for n, how in users)
            out.append(Problem("warning", f"{pin} is used by {who}",
                               [n.path for n, _ in users], kind="pin"))

    # Two enabled devices at one address on a bus; two enabled bus controllers
    # on one register block (nRF's spi0 and i2c0); overlapping flash partitions.
    on_bus: dict[str, dict[int, list]] = {}
    controllers: dict[int, list] = {}
    partitions: dict[str, list] = {}
    for node in edt.nodes:
        if not enabled(node) or not node.regs or node.parent is None:
            continue
        addr = node.regs[0].addr
        if node.parent.buses and addr is not None:
            on_bus.setdefault(node.parent.path, {}).setdefault(addr, []).append(node)
        if node.buses and addr is not None and not node.parent.buses:
            controllers.setdefault(addr, []).append(node)
        if "fixed-partitions" in node.parent.compats:
            partitions.setdefault(node.parent.path, []).append(node)
    for bus_path, by_addr in on_bus.items():
        bus = edt.get_node(bus_path)
        for addr, same in sorted(by_addr.items()):
            if len(same) > 1:
                out.append(Problem("error", f"{', '.join(map(_name, same))} are all at "
                                   f"{addr:#x} on {_name(bus)}", [n.path for n in same],
                                   kind="address"))
    for addr, same in sorted(controllers.items()):
        if len(same) > 1:
            out.append(Problem("error", f"{', '.join(map(_name, same))} share the registers at "
                               f"{addr:#x}; only one of them can be enabled",
                               [n.path for n in same], kind="address"))
    for nodes in partitions.values():
        out += _overlaps(nodes)

    # Enabled nodes that point at disabled ones, and chosen entries that do.
    refs = references(edt) if refs is None else refs
    for target_path, lst in refs.items():
        try:
            target = edt.get_node(target_path)
        except Exception:  # EDTError
            continue
        if enabled(target):
            continue
        for ref in lst:
            if ref.source == "/chosen":
                out.append(Problem("warning", f"chosen {ref.prop} is {_name(target)}, which "
                                   "is disabled", [target_path, "/chosen"], ref.prop,
                                   kind="disabled"))
                continue
            try:
                source = edt.get_node(ref.source)
            except Exception:  # EDTError
                continue
            if ref.source == "/aliases" or not enabled(source) or not source._binding:
                continue
            out.append(Problem("warning", f"{_name(source)} {ref.prop} needs {_name(target)}, "
                               "which is disabled", [ref.source, target_path], ref.prop,
                               kind="disabled"))

    # Deprecated properties that are set.
    for node in edt.nodes:
        for name, prop in node.props.items():
            if prop.spec.deprecated and name in node._node.props:
                out.append(Problem("warning", f"{_name(node)} sets {name}, which is deprecated",
                                   [node.path], name, kind="deprecated"))
    out.sort(key=lambda p: p.severity != "error")
    return out


def _overlaps(nodes: list) -> list[Problem]:
    spans = []
    for n in nodes:
        for r in n.regs:
            if r.addr is not None and r.size:
                spans.append((r.addr, r.addr + r.size, n))
    spans.sort(key=lambda s: (s[0], -s[1]))
    out, seen = [], set()
    for i, (start, end, node) in enumerate(spans):
        for start2, end2, other in spans[i + 1:]:
            if start2 >= end:
                break
            if other is node or node.path.startswith(other.path + "/") or \
                    other.path.startswith(node.path + "/"):
                continue
            key = tuple(sorted((node.path, other.path)))
            if key in seen:
                continue
            seen.add(key)
            out.append(Problem(
                "error", f"{_name(node)} ({start:#x}-{end - 1:#x}) and {_name(other)} "
                f"({start2:#x}-{end2 - 1:#x}) overlap", [node.path, other.path],
                kind="partition"))
    return out
