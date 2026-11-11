"""Changing Kconfig options: edits go into the application's conf files (never
.config, which the build regenerates), then the configuration is evaluated
again to show what the change does before the next build."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import loader

_LINE = re.compile(r"^\s*(?:CONFIG_(\w+)\s*=(.*)|#\s*CONFIG_(\w+) is not set)\s*$")


@dataclass
class Assignment:
    file: Path
    line: int  # 1-based
    value: str  # as written, quotes removed; "n" for "is not set"


@dataclass
class Result:
    ok: bool
    message: str
    # Options that must be turned on first, when ok is False because of them.
    needs: list[str] = field(default_factory=list)


@dataclass
class Problem:
    severity: str  # "error" or "warning"
    message: str
    symbol: str | None
    file: Path | None = None
    line: int | None = None


def assignments(files: list[Path]) -> dict[str, list[Assignment]]:
    """Symbol -> every line in `files` (in order) that sets it."""
    out: dict[str, list[Assignment]] = {}
    for f in files:
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for i, text in enumerate(lines, 1):
            m = _LINE.match(text)
            if not m:
                continue
            if m.group(1):
                name, value = m.group(1), m.group(2).strip()
                if len(value) >= 2 and value[0] == value[-1] == '"':
                    value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            else:
                name, value = m.group(3), "n"
            out.setdefault(name, []).append(Assignment(f, i, value))
    return out


def conf_line(name: str, type_name: str, value: str) -> str:
    if type_name == "string":
        return f'CONFIG_{name}="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"CONFIG_{name}={value}"


def set_in_text(text: str, name: str, line: str | None) -> str:
    """`text` with CONFIG_name set by `line` (replacing an existing assignment),
    or with every assignment of it removed when `line` is None."""
    lines = text.splitlines()
    hits = [i for i, t in enumerate(lines)
            if (m := _LINE.match(t)) and name in (m.group(1), m.group(3))]
    if line is None:
        for i in reversed(hits):
            del lines[i]
    elif hits:
        lines[hits[-1]] = line
        for i in reversed(hits[:-1]):
            del lines[i]
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append(line)
    return "\n".join(lines) + "\n" if lines else ""


class Session:
    def __init__(self, build: loader.BuildKconfig):
        self.build = build
        self.model: loader.Model | None = None
        self.undo_stack: list[list[tuple[Path, str | None]]] = []

    def load(self) -> None:
        """Raises loader.KconfigError."""
        self.model = loader.load(self.build)

    @property
    def kconf(self):
        return self.model.kconf

    def symbol(self, name: str):
        return self.model.symbol(name)

    # -- where things are set -------------------------------------------

    def assignments(self) -> dict[str, list[Assignment]]:
        return assignments(self.build.conf_files)

    def assigned(self, name: str) -> list[Assignment]:
        return assignments(self.build.conf_files).get(name, [])

    def write_file(self, name: str | None = None) -> Path | None:
        """Where a change goes: the user conf file that already sets the option,
        else prj.conf (else the first user conf file)."""
        users = [f for f in self.build.user_files]
        if not users:
            return None
        if name:
            mine = [a.file for a in self.assigned(name) if a.file in users]
            if mine:
                return mine[-1]
        prj = [f for f in users if f.name == "prj.conf"]
        return (prj or users)[0]

    def pending(self) -> list[tuple[str, str, str]]:
        """(symbol, value now, value in the last build) for options a rebuild changes."""
        out = []
        for sym in self.kconf.unique_defined_syms:
            if not sym.nodes or sym.type == 0:
                continue
            built = self.model.built.get(sym.name)
            now = sym.str_value
            if built is None:
                if sym.config_string and now not in ("", "n"):
                    out.append((sym.name, now, "(not set)"))
            elif built != now:
                out.append((sym.name, now, built))
        return out

    # -- editing ---------------------------------------------------------

    def _write(self, changes: dict[Path, str]) -> list[tuple[Path, str | None]]:
        old = []
        for path, text in changes.items():
            old.append((path, path.read_text() if path.exists() else None))
            path.write_text(text)
        return old

    def _restore(self, old: list[tuple[Path, str | None]]) -> None:
        for path, text in old:
            if text is None:
                try:
                    path.unlink()
                except OSError:
                    pass
            else:
                path.write_text(text)

    def set_values(self, values: list[tuple[str, str]], file: Path | None = None) -> Result:
        """Set options (name, value) in one step, e.g. dependencies and then the
        option itself. Rolled back if the last one doesn't end up with its value;
        `needs` then lists what it waits for."""
        if not values:
            return Result(False, "nothing to change")
        changes: dict[Path, str] = {}
        for name, value in values:
            sym = self.symbol(name)
            if sym is None or not sym.nodes:
                return Result(False, f"CONFIG_{name} doesn't exist in this build")
            target = file or self.write_file(name)
            if target is None:
                return Result(False, "this build has no conf file of the application's "
                                     "to write to (prj.conf)")
            text = changes.get(target)
            if text is None:
                text = target.read_text() if target.exists() else ""
            changes[target] = set_in_text(text, name, conf_line(name, _type(self, sym), value))
        old = self._write(changes)
        loader.evaluate(self.model)
        name, value = values[-1]
        sym = self.symbol(name)
        if not _same(sym, value):
            self._restore(old)
            loader.evaluate(self.model)
            return self._why_not(sym, value)
        self.undo_stack.append(old)
        files = ", ".join(sorted({p.name for p in changes}))
        if len(values) == 1:
            return Result(True, f"CONFIG_{name}={value} in {files}")
        others = ", ".join(n for n, _ in values[:-1])
        return Result(True, f"CONFIG_{name}={value} (and {others}) in {files}")

    def remove(self, name: str, file: Path | None = None) -> Result:
        """Remove the option's lines from the user conf files (or `file`)."""
        files = [file] if file else [a.file for a in self.assigned(name)
                                     if a.file in self.build.user_files]
        files = list(dict.fromkeys(files))
        if not files:
            return Result(False, f"CONFIG_{name} isn't set in your conf files")
        changes = {f: set_in_text(f.read_text(), name, None) for f in files}
        old = self._write(changes)
        loader.evaluate(self.model)
        self.undo_stack.append(old)
        sym = self.symbol(name)
        now = sym.str_value if sym is not None else "?"
        return Result(True, f"removed CONFIG_{name} from "
                            f"{', '.join(f.name for f in files)}; it is now {now or '(empty)'}")

    def undo(self) -> Result:
        if not self.undo_stack:
            return Result(False, "nothing to undo")
        self._restore(self.undo_stack.pop())
        loader.evaluate(self.model)
        return Result(True, "undone")

    def _why_not(self, sym, value: str) -> Result:
        lib = self.model.kconfiglib
        name = sym.name
        if not any(node.prompt for node in sym.nodes):
            by = selected_by(self, sym)
            hint = f"; it is turned on by {', '.join(by)}" if by else ""
            return Result(False, f"CONFIG_{name} can't be set directly{hint}")
        if value in ("y", "m") and lib.expr_value(sym.direct_dep) == 0:
            needs, blockers = missing_deps(self, sym)
            if blockers:
                return Result(False, f"CONFIG_{name} depends on {', '.join(blockers)}")
            return Result(False, f"CONFIG_{name} needs {', '.join(needs)}", needs)
        if value == "n" and sym.rev_dep is not None and lib.expr_value(sym.rev_dep):
            return Result(False, f"CONFIG_{name} stays on: it is selected by "
                                 f"{', '.join(selected_by(self, sym, active=True))}")
        if sym.type in (lib.INT, lib.HEX) and sym.ranges:
            return Result(False, f"{value} is out of range for CONFIG_{name}"
                                 f"{range_text(self, sym)}")
        return Result(False, f"CONFIG_{name} didn't become {value} (it is "
                             f"{sym.str_value or 'empty'})")

    # -- problems ----------------------------------------------------------

    def problems(self) -> list[Problem]:
        """Lines in the application's conf files that don't do what they say."""
        out = []
        lib = self.model.kconfiglib
        for name, lst in self.assignments().items():
            mine = [a for a in lst if a.file in self.build.user_files]
            if not mine:
                continue
            a = mine[-1]
            sym = self.symbol(name)
            if sym is None or not sym.nodes:
                out.append(Problem("error", f"{a.file.name}:{a.line}: CONFIG_{name} doesn't "
                                            "exist (misspelled, or its module isn't in this "
                                            "build)", None, a.file, a.line))
                continue
            later = [b for b in lst if lst.index(b) > lst.index(a)]
            if later:
                b = later[-1]
                out.append(Problem("warning", f"{a.file.name}:{a.line}: CONFIG_{name} is set "
                                              f"again in {b.file.name}:{b.line}", name,
                                   a.file, a.line))
                continue
            if not _same(sym, a.value):
                why = self._why_not(sym, a.value).message
                out.append(Problem("warning", f"{a.file.name}:{a.line}: CONFIG_{name}="
                                              f"{a.value} has no effect: {why}",
                                   name, a.file, a.line))
        out.sort(key=lambda p: p.severity != "error")
        return out


def _type(session: Session, sym) -> str:
    return session.model.kconfiglib.TYPE_TO_STR[sym.type]


def _same(sym, value: str) -> bool:
    now = sym.str_value
    if now == value:
        return True
    try:
        return int(now, 0) == int(value, 0)
    except (ValueError, TypeError):
        return False


def missing_deps(session: Session, sym, seen=None) -> tuple[list[str], list[str]]:
    """Options to turn on (in order) so `sym` can be turned on, and the
    dependencies that can't simply be turned on (e.g. `!FOO` or `A || B`)."""
    lib = session.model.kconfiglib
    seen = set() if seen is None else seen
    needs, blockers = [], []
    for term in lib.split_expr(sym.direct_dep, lib.AND):
        if lib.expr_value(term):
            continue
        if isinstance(term, lib.Symbol) and term.type in (lib.BOOL, lib.TRISTATE) \
                and term.name not in seen and any(n.prompt for n in term.nodes):
            seen.add(term.name)
            more, stuck = missing_deps(session, term, seen)
            needs += [n for n in more if n not in needs]
            blockers += stuck
            if term.name not in needs:
                needs.append(term.name)
        else:
            blockers.append(lib.expr_str(term))
    return needs, blockers


def selected_by(session: Session, sym, active: bool = False) -> list[str]:
    """The options with `select <sym>`, e.g. ['SHELL_BACKEND_SERIAL']."""
    lib = session.model.kconfiglib
    out = []
    for term in lib.split_expr(sym.rev_dep, lib.OR):
        if active and not lib.expr_value(term):
            continue
        first = lib.split_expr(term, lib.AND)[0]
        if isinstance(first, lib.Symbol) and first.is_constant:
            continue  # nothing selects it (the expression is just `n`)
        name = first.name if isinstance(first, lib.Symbol) else lib.expr_str(first)
        if name not in out:
            out.append(name)
    return out


def range_text(session: Session, sym) -> str:
    lib = session.model.kconfiglib
    for low, high, cond, *_ in sym.ranges:
        if lib.expr_value(cond):
            return f" ({low.str_value}–{high.str_value})"
    return ""
