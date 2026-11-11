"""Turn what the user types into devicetree property values, by binding type.

The parsed tree is the final judge (every edit is re-parsed and reverted if it
doesn't parse), but checking here gives quicker, clearer messages.
"""

from __future__ import annotations

import re

_NUMBER = re.compile(r"^-?(0[xX][0-9a-fA-F]+|\d+)[uUlL]*$")
_IDENT = re.compile(r"^[A-Za-z_]\w*$")
_STRING = r'"(?:[^"\\]|\\.)*"'
_STRINGS = re.compile(rf"^{_STRING}(\s*,\s*{_STRING})*$")
_HEX_BYTE = re.compile(r"^[0-9a-fA-F]{2}$")

# Types the editor can write. "compound" is accepted as raw text.
EDITABLE = {"boolean", "int", "array", "uint8-array", "string", "string-array", "phandle",
            "phandles", "phandle-array", "path", "compound"}


def _cells(text: str) -> list[str]:
    """Split `1 2 (3 + 4) FOO(5, 6)` into cells, keeping parenthesised groups."""
    cells, depth, cur = [], 0, ""
    for c in text:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if depth == 0 and (c.isspace() or c == ","):
            if cur:
                cells.append(cur)
            cur = ""
            continue
        cur += c
    if depth:
        raise ValueError("unbalanced parentheses")
    if cur:
        cells.append(cur)
    return cells


def _check_cell(cell: str) -> None:
    if _NUMBER.match(cell) or cell.startswith("("):
        return
    m = re.match(r"^([A-Za-z_]\w*)(\(.*\))?$", cell)
    if not m:
        raise ValueError(f"not a number or macro: {cell}")


def _strip(text: str, open_: str, close: str) -> str | None:
    text = text.strip()
    if text.startswith(open_) and text.endswith(close):
        return text[1:-1].strip()
    return None


def _check_labels(refs: list[str], labels: set[str] | None) -> None:
    if labels is None:
        return
    for ref in refs:
        if ref.startswith("{"):
            continue
        if ref not in labels:
            raise ValueError(f"no node with label '{ref}'")


def _quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_value(type_: str, text: str, enum: list | None = None,
                 labels: set[str] | None = None) -> str:
    """DTS right-hand side for `text` of binding type `type_`. Raises ValueError."""
    text = text.strip()
    if type_ == "int":
        inner = _strip(text, "<", ">")
        cells = _cells(inner if inner is not None else text)
        if len(cells) != 1:
            raise ValueError("expected one number")
        _check_cell(cells[0])
        if enum and _NUMBER.match(cells[0]) and int(cells[0].rstrip("uUlL"), 0) not in enum:
            raise ValueError(f"must be one of: {', '.join(map(str, enum))}")
        return f"<{cells[0]}>"
    if type_ == "array":
        inner = _strip(text, "<", ">")
        cells = _cells(inner if inner is not None else text)
        for c in cells:
            _check_cell(c)
        return f"<{' '.join(cells)}>"
    if type_ == "uint8-array":
        inner = _strip(text, "[", "]")
        raw = (inner if inner is not None else text).replace(",", " ")
        cells = []
        for tok in raw.split():
            tok = tok.removeprefix("0x").removeprefix("0X")
            if len(tok) == 1:
                tok = "0" + tok
            if not _HEX_BYTE.match(tok):
                raise ValueError(f"not a hex byte: {tok}")
            cells.append(tok.lower())
        return f"[{' '.join(cells)}]"
    if type_ == "string":
        if len(text) >= 2 and text[0] == text[-1] == '"':
            text = text[1:-1]
        if enum and text not in enum:
            raise ValueError(f"must be one of: {', '.join(map(str, enum))}")
        return _quote(text)
    if type_ == "string-array":
        if _STRINGS.match(text):
            return text
        items = [s.strip() for s in text.split(",") if s.strip()]
        if not items:
            raise ValueError("expected one or more strings")
        if enum:
            bad = [s for s in items if s not in enum]
            if bad:
                raise ValueError(f"{bad[0]} must be one of: {', '.join(map(str, enum))}")
        return ", ".join(_quote(s) for s in items)
    if type_ in ("phandle", "phandles"):
        inner = _strip(text, "<", ">")
        refs = [r.lstrip("&") for r in (inner if inner is not None else text).split()]
        if not refs or (type_ == "phandle" and len(refs) != 1):
            raise ValueError("expected a node label, e.g. &gpio0" if type_ == "phandle"
                             else "expected node labels, e.g. &dma0 &dma1")
        _check_labels(refs, labels)
        return "<" + " ".join(f"&{r}" for r in refs) + ">"
    if type_ == "phandle-array":
        groups = re.findall(r"<([^>]*)>", text) if "<" in text else [text]
        out = []
        for group in groups:
            cells = _cells(group)
            if not cells:
                continue
            if not cells[0].startswith("&"):
                cells[0] = "&" + cells[0]
            _check_labels([cells[0][1:]], labels)
            for c in cells[1:]:
                if c.startswith("&"):
                    _check_labels([c[1:]], labels)
                else:
                    _check_cell(c)
            out.append("<" + " ".join(cells) + ">")
        if not out:
            raise ValueError("expected e.g. <&gpio0 5 GPIO_ACTIVE_LOW>")
        return ", ".join(out)
    if type_ == "path":
        if text.startswith("&"):
            _check_labels([text[1:]], labels)
            return text
        if len(text) >= 2 and text[0] == text[-1] == '"':
            text = text[1:-1]
        if not text.startswith("/"):
            raise ValueError("expected &label or /path/to/node")
        return _quote(text)
    if type_ == "compound":
        if not text:
            raise ValueError("empty value")
        return text
    raise ValueError(f"can't edit properties of type {type_}")


def to_input(type_: str, raw: str | None) -> str:
    """Edit-box text for a value as written in the source (inverse of format_value)."""
    if raw is None:
        return ""
    raw = raw.strip()
    if type_ in ("int", "array", "phandle", "phandles"):
        inner = _strip(raw, "<", ">")
        return inner if inner is not None and "<" not in inner else raw
    if type_ == "uint8-array":
        inner = _strip(raw, "[", "]")
        return inner if inner is not None else raw
    if type_ in ("string", "path"):
        if len(raw) >= 2 and raw[0] == raw[-1] == '"' and _STRINGS.match(raw):
            return raw[1:-1]
        return raw
    if type_ == "string-array":
        if _STRINGS.match(raw):
            items = re.findall(_STRING, raw)
            if all("," not in s for s in items):
                return ", ".join(s[1:-1] for s in items)
        return raw
    return raw


def show(val) -> str:
    """Short display of a parsed (edtlib) property value."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return hex(val) if val > 9 else str(val)
    if isinstance(val, str):
        return _quote(val)
    if isinstance(val, bytes):
        return "[" + " ".join(f"{b:02x}" for b in val) + "]"
    if isinstance(val, list):
        return "<" + " ".join(show(v) for v in val) + ">" if val and isinstance(val[0], int) \
            else ", ".join(show(v) for v in val)
    path = getattr(val, "path", None)  # edtlib Node
    if path:
        labels = getattr(val, "labels", None)
        return f"&{labels[0]}" if labels else path
    node = getattr(val, "controller", None)  # ControllerAndData
    if node is not None:
        data = " ".join(f"{k}={v}" for k, v in (val.data or {}).items())
        return f"<{show(node)} {data}>".replace(" >", ">")
    return str(val)
