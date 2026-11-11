"""Find nodes and properties in devicetree source text, and edit them in place.

dtlib tells us where a node was first defined and where a property was last
set, but a node is usually touched in several files (a .dtsi defines it, the
board .dts and overlays override it through `&label { ... };`). This scanner
reads each source file on its own, keeps the C preprocessor lines as they are,
and resolves every block to a node path with the labels from the parsed tree.
Edits are plain text changes, so the rest of the file stays untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

_PREPROCESSOR = re.compile(
    r"#\s*(include|define|undef|if|ifdef|ifndef|else|elif|endif|error|warning|pragma|line)\b"
    r"|#\s*\d"
)
_LABEL = re.compile(r"^([A-Za-z_]\w*)\s*:\s*")
PROP_NAME = re.compile(r"^[A-Za-z0-9,._+?#-]+$")
NODE_NAME = re.compile(r"^[A-Za-z0-9,._+-]+(@[A-Za-z0-9,._+-]+)?$")
LABEL_NAME = re.compile(r"^[A-Za-z_]\w*$")
_INCLUDE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*[<"]([^>"]+)[>"]', re.M)


@dataclass
class Block:
    """`name { ... };`, `&label { ... };`, `&{/path} { ... };` or `/ { ... };`."""

    path: str | None
    header: str
    labels: list[str]
    start: int
    open: int
    close: int = -1
    end: int = -1
    # True for `name { }` (defines or extends a child by name), False for refs and `/`.
    by_name: bool = False
    line: int = 0


@dataclass
class Statement:
    """`prop = value;`, `prop;`, `/delete-property/ prop;` or `/delete-node/ x;`."""

    node: str | None
    kind: str  # "set", "delete-property", "delete-node"
    name: str
    value: str | None
    start: int
    end: int
    line: int = 0


@dataclass
class SourceFile:
    text: str
    blocks: list[Block] = field(default_factory=list)
    statements: list[Statement] = field(default_factory=list)
    # (#include name, node path) for files included inside a node.
    nested_includes: list[tuple[str, str]] = field(default_factory=list)

    def blocks_for(self, path: str) -> list[Block]:
        return [b for b in self.blocks if b.path == path]

    def props_for(self, path: str, name: str | None = None) -> list[Statement]:
        return [s for s in self.statements
                if s.node == path and s.kind in ("set", "delete-property")
                and (name is None or s.name == name)]


class ScanError(ValueError):
    pass


# --------------------------------------------------------------------------
# Scanning
# --------------------------------------------------------------------------


def _skip_space(text: str, i: int) -> int:
    """Skip whitespace, comments and preprocessor lines."""
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            i = _line_end(text, i)
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == "#" and _at_line_start(text, i) and (m := _PREPROCESSOR.match(text, i)):
            if m.group(1) in ("else", "elif"):
                # Only the first branch of an #if is scanned; braces in the
                # other branches would unbalance the blocks.
                i = _skip_branch(text, i)
                continue
            # Directive, including backslash-continued lines.
            while True:
                j = _line_end(text, i)
                if text[i:j].rstrip().endswith("\\") and j < n:
                    i = j + 1
                    continue
                i = j
                break
        else:
            break
    return i


_CONDITIONAL = re.compile(r"^[ \t]*#[ \t]*(if|ifdef|ifndef|endif)\b", re.M)


def _skip_branch(text: str, i: int) -> int:
    """From an #else/#elif to the end of its #endif line."""
    depth = 0
    for m in _CONDITIONAL.finditer(text, _line_end(text, i)):
        if m.group(1) == "endif":
            if depth == 0:
                return _line_end(text, m.start())
            depth -= 1
        else:
            depth += 1
    return len(text)


def _line_end(text: str, i: int) -> int:
    j = text.find("\n", i)
    return len(text) if j < 0 else j


def _at_line_start(text: str, i: int) -> bool:
    k = text.rfind("\n", 0, i) + 1
    return text[k:i].strip() == ""


def _read_statement(text: str, i: int) -> tuple[int, str]:
    """Index of the `{`, `;` or `}` ending the statement at `i`, and which it was."""
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
            i += 1
        elif c == "'":
            j = text.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif text.startswith("//", i):
            i = _line_end(text, i)
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif text.startswith("&{", i):
            j = text.find("}", i)
            i = n if j < 0 else j + 1
        elif c in "{;}":
            return i, c
        else:
            i += 1
    return n, ""


def _child_path(parent: str | None, name: str) -> str | None:
    if parent is None:
        return None
    return "/" + name if parent == "/" else f"{parent}/{name}"


def _strip_comments(s: str) -> str:
    s = re.sub(r"/\*.*?\*/", " ", s, flags=re.S)
    return re.sub(r"//[^\n]*", " ", s).strip()


def scan(text: str, resolve: Callable[[str], str | None],
         child: Callable[[str, str, list[str]], str | None] | None = None,
         root: str | None = None) -> SourceFile:
    """Scan devicetree source.

    `resolve` maps a label to its node path (or None). `child(parent, name, labels)`
    returns the real path of a child written by name, for names the preprocessor
    changes (e.g. `itcm@ITCM_BASE_NAME`); without it the name is used as written.
    `root` is the node a file is #included into, for files included inside a node.
    """
    sf = SourceFile(text)
    stack: list[Block] = []
    i, n = 0, len(text)
    while True:
        i = _skip_space(text, i)
        if i >= n:
            break
        if text[i] == "}":
            if not stack:
                raise ScanError(f"unbalanced '}}' at line {_line(text, i)}")
            block = stack.pop()
            block.close = i
            j = _skip_space(text, i + 1)
            block.end = j + 1 if j < n and text[j] == ";" else i + 1
            sf.blocks.append(block)
            i = block.end
            continue
        if text.startswith("/include/", i):
            i = _line_end(text, i)
            continue
        start = i
        j, term = _read_statement(text, i)
        header = _strip_comments(text[start:j])
        parent = stack[-1].path if stack else root
        if term == "{":
            block = _block(header, parent, start, j, resolve, bool(stack) or root is not None,
                           _line(text, start))
            if block.by_name and child and parent is not None:
                block.path = child(parent, block.header, block.labels)
            stack.append(block)
            i = j + 1
        elif term == ";":
            stmt = _statement(header, parent, start, j + 1, _line(text, start))
            if stmt:
                sf.statements.append(stmt)
            i = j + 1
        elif term == "}":
            # A statement without ';' right before '}' (invalid, but don't loop).
            i = j
        else:
            break
    if stack:
        raise ScanError(f"unclosed block '{stack[-1].header}' from line {stack[-1].line}")
    sf.blocks.sort(key=lambda b: b.start)
    for m in _INCLUDE.finditer(text):
        inside = [b for b in sf.blocks if b.open < m.start() < b.close]
        node = inside[-1].path if inside else root
        if node:
            sf.nested_includes.append((m.group(1), node))
    return sf


def _line(text: str, i: int) -> int:
    return text.count("\n", 0, i) + 1


def _block(header: str, parent: str | None, start: int, open_: int,
           resolve: Callable[[str], str | None], nested: bool, line: int) -> Block:
    header = header.replace("/omit-if-no-ref/", "").strip()
    labels = []
    while m := _LABEL.match(header):
        labels.append(m.group(1))
        header = header[m.end():]
    name = header.strip()
    if name == "/":
        path, by_name = "/", False
    elif name.startswith("&{") and name.endswith("}"):
        path, by_name = name[2:-1].strip() or None, False
    elif name.startswith("&"):
        path, by_name = resolve(name[1:].strip()), False
    else:
        path, by_name = _child_path(parent if nested else None, name), True
    return Block(path, name, labels, start, open_, by_name=by_name, line=line)


def _statement(header: str, parent: str | None, start: int, end: int,
               line: int) -> Statement | None:
    if header.startswith("/delete-property/"):
        return Statement(parent, "delete-property", header.split(None, 1)[-1].strip(), None,
                         start, end, line)
    if header.startswith("/delete-node/"):
        return Statement(parent, "delete-node", header.split(None, 1)[-1].strip(), None,
                         start, end, line)
    if header.startswith("/") or not header:
        return None  # /dts-v1/, /plugin/, /memreserve/
    while m := _LABEL.match(header):
        header = header[m.end():]
    name, eq, value = header.partition("=")
    name = name.strip()
    if not PROP_NAME.match(name):
        return None
    return Statement(parent, "set", name, value.strip() if eq else None, start, end, line)


# --------------------------------------------------------------------------
# Editing
# --------------------------------------------------------------------------


def node_ref(path: str, labels: list[str]) -> str:
    """How an overlay refers to an existing node."""
    if path == "/":
        return "/"
    return f"&{labels[0]}" if labels else f"&{{{path}}}"


def prop_text(name: str, value: str | None) -> str:
    return f"{name};" if value is None else f"{name} = {value};"


def _line_start(text: str, i: int) -> int:
    return text.rfind("\n", 0, i) + 1


def _indent_of(text: str, i: int) -> str:
    start = _line_start(text, i)
    j = start
    while j < len(text) and text[j] in " \t":
        j += 1
    return text[start:j]


def _body_indent(sf: SourceFile, block: Block) -> str:
    """Indentation used for statements inside `block`."""
    text = sf.text
    j = _skip_space(text, block.open + 1)
    if j < block.close and text[block.open + 1:j].count("\n"):
        return _indent_of(text, j)
    return _indent_of(text, block.start) + "\t"


def _insert_in_block(sf: SourceFile, block: Block, lines: list[str], blank_before=False) -> str:
    """Insert `lines` (unindented) at the end of `block`."""
    text = sf.text
    indent = _body_indent(sf, block)
    body = "".join(f"{indent}{ln}\n" if ln else "\n" for ln in lines)
    ls = _line_start(text, block.close)
    if text[ls:block.close].strip() == "":
        prev = text[:ls].rstrip(" \t")
        if blank_before and not prev.endswith("{\n") and not prev.endswith("\n\n"):
            body = "\n" + body
        return text[:ls] + body + text[ls:]
    # Closing brace shares a line with other text, e.g. `foo { };`.
    return (text[:block.close].rstrip(" \t") + "\n" + body
            + _indent_of(text, block.start) + text[block.close:])


def _append_block(text: str, ref: str, lines: list[str]) -> str:
    body = "".join(f"\t{ln}\n" if ln else "\n" for ln in lines)
    head = text.rstrip("\n")
    sep = "\n\n" if head else ""
    return f"{head}{sep}{ref} {{\n{body}}};\n"


def _remove_range(text: str, start: int, end: int) -> str:
    """Remove text[start:end], plus its line if nothing else is left on it."""
    ls = _line_start(text, start)
    le = _line_end(text, end)
    if text[ls:start].strip() == "" and text[end:le].strip() == "":
        end = min(le + 1, len(text))
        start = ls
        # Don't leave two blank lines behind.
        if text[start:].startswith("\n") and text[:start].endswith("\n\n"):
            end = min(end + 1, len(text)) if text[end:end + 1] == "\n" else end
    return text[:start] + text[end:]


def set_property(sf: SourceFile, path: str, labels: list[str], name: str,
                 value: str | None) -> str:
    """Set `name` (value None: boolean true) on node `path` in this file."""
    stmts = [s for s in sf.props_for(path, name)]
    line = prop_text(name, value)
    if stmts and stmts[-1].kind == "set":
        s = stmts[-1]
        return sf.text[:s.start] + line + sf.text[s.end:]
    if stmts:  # last one deletes it: put the new value after that
        s = stmts[-1]
        indent = _indent_of(sf.text, s.start)
        le = _line_end(sf.text, s.end)
        return sf.text[:le] + "\n" + indent + line + sf.text[le:]
    blocks = sf.blocks_for(path)
    if blocks:
        return _insert_in_block(sf, blocks[-1], [line])
    return _append_block(sf.text, node_ref(path, labels), [line])


def delete_property(sf: SourceFile, path: str, labels: list[str], name: str) -> str:
    """Add `/delete-property/ name;` to node `path` in this file."""
    line = f"/delete-property/ {name};"
    blocks = sf.blocks_for(path)
    if blocks:
        return _insert_in_block(sf, blocks[-1], [line])
    return _append_block(sf.text, node_ref(path, labels), [line])


def remove_statement(sf: SourceFile, stmt: Statement) -> str:
    return _remove_range(sf.text, stmt.start, stmt.end)


def remove_block(sf: SourceFile, block: Block) -> str:
    return _remove_range(sf.text, block.start, block.end)


def node_lines(name: str, label: str | None, props: list[tuple[str, str | None]]) -> list[str]:
    head = f"{label}: {name} {{" if label else f"{name} {{"
    return [head, *(f"\t{prop_text(k, v)}" for k, v in props), "};"]


def add_node(sf: SourceFile, parent: str, parent_labels: list[str], lines: list[str]) -> str:
    """Add a child node (lines from node_lines()) under `parent` in this file."""
    blocks = sf.blocks_for(parent)
    if blocks:
        return _insert_in_block(sf, blocks[-1], lines, blank_before=True)
    return _append_block(sf.text, node_ref(parent, parent_labels), lines)


def delete_node_stmt(sf: SourceFile, path: str, labels: list[str]) -> str:
    """Add `/delete-node/ &label;` (or the path form) at the end of the file."""
    ref = f"&{labels[0]}" if labels else f"&{{{path}}}"
    head = sf.text.rstrip("\n")
    return f"{head}\n\n/delete-node/ {ref};\n"
