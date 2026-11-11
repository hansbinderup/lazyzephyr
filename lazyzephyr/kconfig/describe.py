"""Kconfig symbols in words: their value, why they have it, what they depend
on, what selects them and what they select."""

from __future__ import annotations

from pathlib import Path

from rich.text import Text

from .session import Session, _same, missing_deps, range_text, selected_by


def rel(path: str | Path, base: Path | None) -> str:
    p = Path(path)
    if base is not None:
        try:
            return str(p.resolve().relative_to(base))
        except (ValueError, OSError):
            pass
    return str(p)


def loc(session: Session, file: str | Path, base: Path | None) -> str:
    """A Kconfig location (relative to Zephyr's source tree) for display."""
    p = Path(file)
    if not p.is_absolute():
        p = Path(session.build.env.get("srctree", "")) / p
    return rel(p, base)


def lib_of(session: Session):
    return session.model.kconfiglib


def is_bool(session: Session, sym) -> bool:
    lib = lib_of(session)
    return sym.type in (lib.BOOL, lib.TRISTATE)


def can_set(sym) -> bool:
    """Has a prompt somewhere, so a conf file can set it (if its deps allow)."""
    return any(node.prompt for node in sym.nodes)


def visible(session: Session, node) -> bool:
    lib = lib_of(session)
    return bool(node.prompt) and lib.expr_value(node.prompt[1]) > 0


def value_text(session: Session, sym) -> Text:
    """`● on`, `○ off`, `115200`, `"text"`."""
    lib = lib_of(session)
    if isinstance(sym, lib.Choice):
        sel = sym.selection
        return Text(prompt_of(sel) or sel.name, style="yellow") if sel else \
            Text("nothing chosen", style="bright_black")
    if is_bool(session, sym):
        v = sym.str_value
        if v == "y":
            return Text("● on", style="green")
        if v == "m":
            return Text("◐ module", style="yellow")
        return Text("○ off", style="bright_black")
    if sym.type == lib.STRING:
        return Text(f'"{sym.str_value}"' if sym.str_value else "(empty)",
                    style="" if sym.str_value else "bright_black")
    if sym.str_value == "":
        return Text("(not set)", style="bright_black")
    return Text(sym.str_value)


def prompt_of(item) -> str:
    for node in getattr(item, "nodes", ()):
        if node.prompt:
            return node.prompt[0]
    return ""


def type_name(session: Session, sym) -> str:
    return lib_of(session).TYPE_TO_STR[sym.type]


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------


def expr_text(session: Session, expr) -> Text:
    """An expression with each option coloured by its value: on green, off dim."""
    lib = lib_of(session)
    t = Text()

    def sym_part(item) -> None:
        if isinstance(item, lib.Symbol) and item.is_constant:
            t.append(item.name, style="cyan")
            return
        name = item.name if isinstance(item, lib.Symbol) else f"<choice {item.name}>"
        on = lib.expr_value(item) > 0 if isinstance(item, (lib.Symbol, lib.Choice)) else False
        if isinstance(item, lib.Symbol) and item.type not in (lib.BOOL, lib.TRISTATE, 0):
            t.append(name, style="yellow")
            return
        t.append(name, style="green" if on else "red")

    ops = {lib.EQUAL: "=", lib.UNEQUAL: "!=", lib.LESS: "<", lib.LESS_EQUAL: "<=",
           lib.GREATER: ">", lib.GREATER_EQUAL: ">="}

    def walk(e, parent_op=None) -> None:
        if not isinstance(e, tuple):
            sym_part(e)
            return
        op = e[0]
        if op == lib.NOT:
            t.append("!")
            walk(e[1], lib.NOT)
            return
        if op in ops:
            walk(e[1], op)
            t.append(f" {ops[op]} ")
            walk(e[2], op)
            return
        paren = parent_op is not None and parent_op != op
        if paren:
            t.append("(")
        walk(e[1], op)
        t.append(" && " if op == lib.AND else " || ")
        walk(e[2], op)
        if paren:
            t.append(")")

    walk(expr)
    return t


def or_terms(session: Session, expr) -> list:
    lib = lib_of(session)
    return lib.split_expr(expr, lib.OR)


# --------------------------------------------------------------------------
# Why a symbol has its value
# --------------------------------------------------------------------------


def _default_here(session: Session, sym):
    """(value text, "file:line") of the default in effect, or None."""
    lib = lib_of(session)
    for node in sym.nodes:
        for value, cond, *loc in node.defaults:
            if lib.expr_value(cond):
                v = value.str_value if isinstance(value, lib.Symbol) else lib.expr_str(value)
                # Zephyr's kconfiglib records where each default is.
                if loc and isinstance(loc[0], tuple) and len(loc[0]) == 2:
                    return v, loc[0]
                return v, (node.filename, node.linenr)
    return None


def why(session: Session, sym, base: Path | None) -> list[Text]:
    """Why `sym` has its value, most important first."""
    lib = lib_of(session)
    out = []
    lst = session.assigned(sym.name)
    users = set(session.build.user_files)
    if lst:
        a = lst[-1]
        who = "your" if a.file in users else "the board's"
        where = f"{rel(a.file, base)}:{a.line}"
        if _same(sym, a.value):
            out.append(Text(f"Set in {who} {where}", style="green" if a.file in users else ""))
        else:
            r = session._why_not(sym, a.value)
            t = Text(f"{where} sets {a.value}, but it has no effect: ", style="red")
            t.append(r.message.removeprefix(f"CONFIG_{sym.name} "), style="red")
            out.append(t)
    if sym.rev_dep is not None and lib.expr_value(sym.rev_dep):
        by = selected_by(session, sym, active=True)
        out.append(Text(f"Turned on by {', '.join(by)} (select)", style="yellow"))
    elif getattr(sym, "weak_rev_dep", None) is not None and lib.expr_value(sym.weak_rev_dep) \
            and is_bool(session, sym) and sym.str_value != "n" and not lst:
        by = implied_by(session, sym, active=True)
        out.append(Text(f"Turned on by {', '.join(by)} (imply)", style="yellow"))
    if not out or (lst and not _same(sym, lst[-1].value)):
        d = _default_here(session, sym)
        if sym.choice is not None:
            sel = sym.choice.selection
            out.append(Text(f"Part of the choice \"{prompt_of(sym.choice)}\"; "
                            f"{'this one is chosen' if sel is sym else 'not chosen'}"))
        elif d is not None:
            v, (file, line) = d
            out.append(Text(f"Default {v}  ({loc(session, file, base)}:{line})", style="bright_black"))
        elif not out:
            out.append(Text("Not set, and no default applies", style="bright_black"))
    if not can_set(sym):
        out.append(Text("Can't be set in a conf file (no prompt): only select or a default "
                        "change it", style="bright_black"))
    built = session.model.built.get(sym.name)
    if built is not None and not _same(sym, built) or \
            built is None and sym.str_value not in ("", "n") and sym.config_string:
        out.append(Text(f"Not built yet: the last build had {built or '(not set)'}; "
                        "rebuild to apply", style="magenta"))
    return out


def implied_by(session: Session, sym, active: bool = False) -> list[str]:
    lib = lib_of(session)
    out = []
    for term in lib.split_expr(sym.weak_rev_dep, lib.OR):
        if active and not lib.expr_value(term):
            continue
        first = lib.split_expr(term, lib.AND)[0]
        if isinstance(first, lib.Symbol) and first.is_constant:
            continue
        name = first.name if isinstance(first, lib.Symbol) else lib.expr_str(first)
        if name not in out:
            out.append(name)
    return out


def selects(session: Session, sym) -> list[tuple[str, bool]]:
    """(option, whether the select is in effect) for `select X` lines of `sym`."""
    lib = lib_of(session)
    on = lib.expr_value(sym) > 0
    out = []
    for node in sym.nodes:
        for target, cond, *_ in node.selects:
            out.append((target.name, on and lib.expr_value(cond) > 0))
    return out


def implies(session: Session, sym) -> list[tuple[str, bool]]:
    lib = lib_of(session)
    on = lib.expr_value(sym) > 0
    out = []
    for node in sym.nodes:
        for target, cond, *_ in node.implies:
            out.append((target.name, on and lib.expr_value(cond) > 0))
    return out


# --------------------------------------------------------------------------
# The details panel
# --------------------------------------------------------------------------


def details(session: Session, item, base: Path | None) -> Text:
    lib = lib_of(session)
    t = Text()
    if isinstance(item, lib.MenuNode):
        return _node_details(session, item, base)
    if isinstance(item, lib.Choice):
        t.append(prompt_of(item) or "choice", style="bold")
        t.append("\nchoice: ", style="bright_black")
        t.append_text(value_text(session, item))
        t.append("\n\nOptions\n", style="bold")
        for sym in item.syms:
            mark = "● " if item.selection is sym else "○ "
            t.append(f"  {mark}{prompt_of(sym) or sym.name}",
                     style="green" if item.selection is sym else "")
            t.append(f"  {sym.name}\n", style="bright_black")
        _help(t, item)
        _defined(t, item, base, session)
        return t

    sym = item
    t.append(f"CONFIG_{sym.name}", style="bold")
    t.append(f"  {type_name(session, sym)}\n", style="bright_black")
    if prompt_of(sym):
        t.append(prompt_of(sym) + "\n")
    t.append("\nvalue: ", style="bright_black")
    t.append_text(value_text(session, sym))
    if sym.type in (lib.INT, lib.HEX) and sym.ranges:
        t.append(f"   range{range_text(session, sym)}", style="bright_black")
    t.append("\n")
    for line in why(session, sym, base):
        t.append("  ")
        t.append_text(line)
        t.append("\n")

    dep = sym.direct_dep
    if not (isinstance(dep, lib.Symbol) and dep is session.kconf.y):
        met = lib.expr_value(dep) > 0
        t.append("\nDepends on ", style="bold")
        t.append("(met)\n" if met else "(not met)\n", style="green" if met else "red")
        terms = or_terms(session, dep)
        if len(terms) == 1:
            t.append("  ")
            t.append_text(expr_text(session, dep))
            t.append("\n")
        else:
            terms.sort(key=lambda e: lib.expr_value(e) == 0)
            for term in terms[:6]:
                t.append("  ✓ " if lib.expr_value(term) else "  ✗ ",
                         style="green" if lib.expr_value(term) else "red")
                t.append_text(expr_text(session, term))
                t.append("\n")
            if len(terms) > 6:
                t.append(f"  … or {len(terms) - 6} more alternatives\n", style="bright_black")
        if not met and is_bool(session, sym):
            needs, blockers = missing_deps(session, sym)
            if needs and not blockers:
                t.append(f"  to turn it on, also turn on: {', '.join(needs)}\n", style="yellow")
    for title, items in (("Selects", selects(session, sym)), ("Implies", implies(session, sym))):
        if items:
            t.append(f"\n{title}\n", style="bold")
            for name, active in items:
                t.append(f"  {name}", style="green" if active else "bright_black")
                if not active:
                    t.append("  (not now)", style="bright_black italic")
                t.append("\n")
    by = selected_by(session, sym)
    if by:
        active = set(selected_by(session, sym, active=True))
        by.sort(key=lambda n: n not in active)
        t.append("\nSelected by\n", style="bold")
        for name in by[:12]:
            t.append(f"  {name}", style="green" if name in active else "bright_black")
            t.append("\n")
        if len(by) > 12:
            t.append(f"  … and {len(by) - 12} more\n", style="bright_black")
    imp = implied_by(session, sym) if getattr(sym, "weak_rev_dep", None) is not None else []
    if imp:
        t.append("\nImplied by\n", style="bold")
        t.append("  " + ", ".join(imp[:12]) + ("…" if len(imp) > 12 else "") + "\n",
                 style="bright_black")
    _help(t, sym)
    _defined(t, sym, base, session)
    return t


def _node_details(session: Session, node, base: Path | None) -> Text:
    t = Text()
    title = node.prompt[0] if node.prompt else "(menu)"
    t.append(title + "\n", style="bold")
    lib = lib_of(session)
    if node.dep is not None and not (isinstance(node.dep, lib.Symbol) and
                                     node.dep is session.kconf.y):
        t.append("\nShown when ", style="bright_black")
        t.append_text(expr_text(session, node.dep))
        t.append("\n")
    if getattr(node, "help", None):
        t.append("\n" + node.help.strip() + "\n", style="bright_black")
    t.append(f"\n{loc(session, node.filename, base)}:{node.linenr}", style="bright_black")
    return t


def _help(t: Text, item) -> None:
    texts = [n.help.strip() for n in item.nodes if getattr(n, "help", None)]
    if texts:
        t.append("\nHelp\n", style="bold")
        t.append(texts[0] + "\n", style="bright_black")


def _defined(t: Text, item, base: Path | None, session: Session) -> None:
    t.append("\nDefined in\n", style="bold")
    for node in item.nodes[:5]:
        t.append(f"  {loc(session, node.filename, base)}:{node.linenr}\n", style="bright_black")


# --------------------------------------------------------------------------
# Related options (the list under the details)
# --------------------------------------------------------------------------


def related(session: Session, sym) -> list[tuple[str, str, Text]]:
    """(kind, option name, row text) for options to jump to."""
    lib = lib_of(session)
    if not isinstance(sym, lib.Symbol):
        if isinstance(sym, lib.Choice):
            return [("option", s.name, _row("option", s, session)) for s in sym.syms]
        return []
    out = []
    seen = set()

    def add(kind: str, name: str) -> None:
        other = session.symbol(name)
        if other is None or not other.nodes or (kind, name) in seen or other is sym:
            return
        seen.add((kind, name))
        out.append((kind, name, _row(kind, other, session)))

    for item in lib.expr_items(sym.direct_dep):
        if isinstance(item, lib.Symbol) and not item.is_constant:
            add("depends on", item.name)
    for name, _ in selects(session, sym):
        add("selects", name)
    active = set(selected_by(session, sym, active=True))
    for name in sorted(selected_by(session, sym), key=lambda n: n not in active):
        add("selected by", name)
    for name, _ in implies(session, sym):
        add("implies", name)
    if getattr(sym, "weak_rev_dep", None) is not None:
        for name in implied_by(session, sym):
            add("implied by", name)
    return out


def _row(kind: str, sym, session: Session) -> Text:
    t = Text()
    t.append(kind.ljust(12), style="bright_black")
    t.append(sym.name.ljust(30)[:30], style="bold")
    t.append(" ")
    t.append_text(value_text(session, sym))
    prompt = prompt_of(sym)
    if prompt:
        t.append(f"  {prompt[:24] + '…' if len(prompt) > 24 else prompt}",
                 style="bright_black")
    t.no_wrap = True
    t.overflow = "ellipsis"
    return t
