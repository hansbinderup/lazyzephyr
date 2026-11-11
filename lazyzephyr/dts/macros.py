"""Macros from the dt-bindings headers a target includes, offered while typing values.

The headers are the ones the preprocessor actually pulled in (the model's deps), so
only macros the devicetree can use are offered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_DEFINE = re.compile(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z]\w*)(\([^)]*\))?[ \t]*(.*?)[ \t]*$",
                     re.M)
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_WORD = re.compile(r"[a-z0-9]+")
# Words too common to say anything about which header fits.
_NOISE = {"zephyr", "include", "dt", "bindings", "h", "cells", "flags", "cell"}
# Helper macros (bit masks and positions) are rarely what goes in a cell.
_HELPER = re.compile(r"_(MASK|SHIFT|POS|MSK|OFFSET)(_|$)")


@dataclass(frozen=True)
class Macro:
    name: str
    value: str
    header: Path
    args: str | None = None  # "(port, pin)" for function-like macros

    @property
    def insert(self) -> str:
        return self.name + ("(" if self.args else "")


def is_binding_header(path: Path) -> bool:
    return path.suffix == ".h" and "dt-bindings" in path.parts


def scan(headers: list[Path]) -> list[Macro]:
    """Every useful #define in `headers`, in file order."""
    out, seen = [], set()
    for header in headers:
        try:
            text = header.read_text(errors="replace")
        except OSError:
            continue
        text = _COMMENT.sub("", text.replace("\\\n", " "))
        for m in _DEFINE.finditer(text):
            name, args, value = m.group(1), m.group(2), " ".join(m.group(3).split())
            # Include guards and empty flag macros have no value worth offering.
            if not value or name in seen:
                continue
            seen.add(name)
            out.append(Macro(name, value, header, args))
    return out


def context_words(*texts: str | None) -> set[str]:
    """Words that hint at which headers fit: `nordic,nrf-saadc` -> {nordic, nrf, saadc}."""
    words = set()
    for t in texts:
        for w in _WORD.findall((t or "").lower()):
            if len(w) >= 3 and w not in _NOISE:
                words.add(w)
    return words


def value_of(m: Macro) -> int | None:
    """The macro's value if it is a plain number, e.g. 400000 or 0x10U."""
    text = m.value.strip()
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    try:
        return int(text.rstrip("uUlL"), 0)
    except ValueError:
        return None


def rank(macros: list[Macro], token: str, context: set[str], limit: int = 50,
         primary: set[str] | None = None) -> list[Macro]:
    """Macros matching `token` (prefix before substring), the ones that fit the
    context first (and those fitting `primary`, e.g. the bus, before those),
    keeping header order otherwise. With no token, only the ones that fit."""
    primary = primary or set()
    tok = token.lower()
    scored = []
    for i, m in enumerate(macros):
        name = m.name.lower()
        if tok and not name.startswith(tok):
            if tok not in name:
                continue
            kind = 1
        else:
            kind = 0
        hay = context_words(str(m.header.name), m.header.parent.name, m.name.replace("_", " "))
        fits = 0 if hay & primary else 1 if hay & context else 2
        if fits == 2 and not tok:
            continue
        scored.append((kind, fits, bool(_HELPER.search(m.name)), i, m))
    scored.sort(key=lambda s: s[:4])
    return [s[4] for s in scored[:limit]]
