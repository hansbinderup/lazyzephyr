"""Base for a tool shown as one tab of the lazyzephyr app.

Each tool (tests, devicetree) started life as its own Textual App. As a View it
keeps that code mostly as is: the few App-only calls are forwarded to the real
app, and child widgets forward their actions to the View that owns them.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import ClassVar

from textual import events
from textual.binding import Binding
from textual.containers import Vertical
from textual.widget import Widget

# Width below which "auto" stacks the columns (see View.portrait_for).
PORTRAIT_MAX_WIDTH = 110
LAYOUT_MODES = ("auto", "landscape", "portrait")
MIN_PANEL = {"horizontal": 12, "vertical": 3}   # cells a drag or key leaves a panel
KEY_STEP = {"horizontal": 4, "vertical": 2}


def layout_file() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "lazyzephyr" / "layout.json"


class LayoutStore:
    """Panel sizes, order and layout mode per tab, in ~/.cache/lazyzephyr/layout.json."""

    def __init__(self, path: Path | None = None):
        self.path = path or layout_file()
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        self.data: dict = data if isinstance(data, dict) else {}

    def tab(self, name: str) -> dict:
        tab = self.data.get(name)
        if not isinstance(tab, dict):
            tab = self.data[name] = {}
        for key in ("sizes", "order"):
            if not isinstance(tab.get(key), dict):
                tab[key] = {}
        return tab

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)
        except OSError:
            pass

# Styles every tab shares.
COMMON_CSS = """
    Screen { layout: vertical; }
    .panel {
        border: round $panel-lighten-2;
        border-title-color: $text-muted;
        border-subtitle-color: $text-muted;
        background: $surface;
    }
    .panel:focus, .panel:focus-within {
        border: round $success;
        border-title-color: $success;
        border-title-style: bold;
    }
"""


class View(Vertical):
    """A whole tool in one widget.

    Panels can be resized by dragging the border between two of them, or with
    +/- on the focused panel, and moved with < / >. SPLITS names the containers
    whose children take part; what you change is remembered per tab.
    """

    SPLITS: ClassVar[tuple[str, ...]] = ()
    AUTO_PORTRAIT: ClassVar[bool] = True   # toggle -portrait on resize (else the tab does)

    BINDINGS = [
        Binding("plus", "panel_grow(1)", "Grow panel", show=False),
        Binding("minus", "panel_grow(-1)", "Shrink panel", show=False),
        Binding("less_than_sign", "panel_move(-1)", "Move panel back", show=False),
        Binding("greater_than_sign", "panel_move(1)", "Move panel on", show=False),
        Binding("equals_sign", "panel_reset", "Reset layout", show=False),
        Binding("v", "cycle_layout", "Layout", show=False),
    ]

    _layout_store: ClassVar[LayoutStore | None] = None

    # -- layout: state --

    @property
    def layout_store(self) -> LayoutStore:
        # Shared by every tab; read again when the cache folder changes (tests).
        store = View._layout_store
        if store is None or store.path != layout_file():
            store = View._layout_store = LayoutStore()
        return store

    @property
    def layout_name(self) -> str:
        return type(self).__name__

    @property
    def layout_state(self) -> dict:
        return self.layout_store.tab(self.layout_name)

    @property
    def layout_mode(self) -> str:
        mode = self.layout_state.get("mode")
        return mode if mode in LAYOUT_MODES else "auto"

    @layout_mode.setter
    def layout_mode(self, mode: str) -> None:
        self.layout_state["mode"] = mode
        self.layout_store.save()

    def portrait_for(self, width: int) -> bool:
        mode = self.layout_mode
        return mode == "portrait" or (mode == "auto" and width < PORTRAIT_MAX_WIDTH)

    def _split_containers(self) -> list[Widget]:
        out = []
        for cid in self.SPLITS:
            found = self.query(f"#{cid}")
            if found:
                out.append(found.first())
        return out

    @staticmethod
    def _panels_shown(container: Widget) -> list[Widget]:
        return [w for w in container.children if w.display and w.id]

    @staticmethod
    def _axis(container: Widget) -> str:
        layout = container.styles.layout
        return "horizontal" if layout is not None and layout.name == "horizontal" \
            else "vertical"

    def _size_key(self, container: Widget) -> str:
        ids = ",".join(sorted(w.id for w in self._panels_shown(container)))
        return f"{container.id}:{self._axis(container)}:{ids}"

    # -- layout: applying --

    def _layout_mount(self) -> None:
        self._sized: set[tuple[Widget, str]] = set()
        self._drag: dict | None = None
        self._default_order = {c.id: [w.id for w in c.children]
                               for c in self._split_containers()}
        self._apply_order()
        if self.AUTO_PORTRAIT:
            self.set_class(self.portrait_for(self.app.size.width), "-portrait")
        self.call_after_refresh(self.apply_panel_layout)

    def on_mount(self) -> None:
        self._layout_mount()

    def _apply_order(self, orders: dict | None = None) -> None:
        orders = self.layout_state["order"] if orders is None else orders
        for c in self._split_containers():
            want = orders.get(c.id)
            if not isinstance(want, list):
                continue
            kids = {w.id: w for w in c.children}
            if set(want) != set(kids):
                continue          # the panels changed since: keep the default
            for i, wid in enumerate(want):
                if c.children[i] is not kids[wid]:
                    c.move_child(kids[wid], before=c.children[i])

    def apply_panel_layout(self) -> None:
        """Put saved sizes on the panels shown now (CSS sizes where none are saved).
        Call after the orientation or the set of shown panels changes."""
        if not hasattr(self, "_sized"):
            return
        for w, dim in self._sized:
            setattr(w.styles, dim, None)
        self._sized = set()
        sizes = self.layout_state["sizes"]
        for c in self._split_containers():
            saved = sizes.get(self._size_key(c))
            if isinstance(saved, dict):
                self._set_sizes(c, {w: saved[w.id] for w in self._panels_shown(c)
                                    if isinstance(saved.get(w.id), (int, float))})

    def _set_sizes(self, container: Widget, sizes: dict[Widget, float]) -> None:
        dim = "width" if self._axis(container) == "horizontal" else "height"
        for w, n in sizes.items():
            setattr(w.styles, dim, f"{max(n, 1):g}fr")
            self._sized.add((w, dim))

    def _current_sizes(self, container: Widget) -> dict[Widget, int]:
        horizontal = self._axis(container) == "horizontal"
        return {w: (w.outer_size.width if horizontal else w.outer_size.height)
                for w in self._panels_shown(container)}

    def _save_sizes(self, container: Widget, sizes: dict[Widget, float]) -> None:
        self.layout_state["sizes"][self._size_key(container)] = \
            {w.id: round(n, 1) for w, n in sizes.items()}
        self.layout_store.save()

    def _resize_pair(self, container: Widget, a: Widget, b: Widget, delta: float,
                     sizes: dict[Widget, float]) -> dict[Widget, float]:
        """Move `delta` cells from b to a (negative: a to b), keeping both usable."""
        low = MIN_PANEL[self._axis(container)]
        total = sizes[a] + sizes[b]
        new_a = min(max(sizes[a] + delta, low), total - low)
        if total < 2 * low:
            new_a = sizes[a]
        sizes = dict(sizes)
        sizes[a], sizes[b] = new_a, total - new_a
        return sizes

    def on_resize(self, event: events.Resize) -> None:
        if self.AUTO_PORTRAIT:
            self.set_class(self.portrait_for(event.size.width), "-portrait")
        self.call_after_refresh(self.apply_panel_layout)

    # -- layout: keys --

    def _focused_panel(self) -> tuple[Widget, Widget] | None:
        """The innermost split container with 2+ panels around the focus, and the
        panel in it that holds the focus."""
        node = self.focused
        if node is None or self not in node.ancestors:
            return None
        containers = self._split_containers()
        child = node
        while child is not None and child is not self:
            parent = child.parent
            if parent in containers and child in self._panels_shown(parent) \
                    and len(self._panels_shown(parent)) > 1:
                return parent, child
            child = parent
        return None

    def action_panel_grow(self, direction: int) -> None:
        found = self._focused_panel()
        if found is None or self.screen.maximized is not None:
            return
        container, panel = found
        shown = self._panels_shown(container)
        i = shown.index(panel)
        other = shown[i + 1] if i + 1 < len(shown) else shown[i - 1]
        sizes = self._current_sizes(container)
        step = KEY_STEP[self._axis(container)] * direction
        sizes = self._resize_pair(container, panel, other, step, sizes)
        self._set_sizes(container, sizes)
        self._save_sizes(container, sizes)

    def action_panel_move(self, direction: int) -> None:
        found = self._focused_panel()
        if found is None or self.screen.maximized is not None:
            return
        container, panel = found
        shown = self._panels_shown(container)
        i = shown.index(panel) + direction
        if not 0 <= i < len(shown):
            return
        if direction < 0:
            container.move_child(panel, before=shown[i])
        else:
            container.move_child(panel, after=shown[i])
        self.layout_state["order"][container.id] = [w.id for w in container.children]
        self.layout_store.save()
        self.call_after_refresh(self.apply_panel_layout)

    def action_panel_reset(self) -> None:
        state = self.layout_state
        state["sizes"], state["order"] = {}, {}
        self.layout_store.save()
        self._apply_order(self._default_order)
        self.call_after_refresh(self.apply_panel_layout)
        self.notify("layout reset", timeout=2)

    def action_cycle_layout(self) -> None:
        mode = LAYOUT_MODES[(LAYOUT_MODES.index(self.layout_mode) + 1) % len(LAYOUT_MODES)]
        self.layout_mode = mode
        portrait = self.portrait_for(self.size.width)
        self.set_class(portrait, "-portrait")
        self.call_after_refresh(self.apply_panel_layout)
        self.notify(f"layout: {mode} ({'stacked' if portrait else 'side by side'})",
                    timeout=2)

    # -- layout: dragging a border --

    def _border_at(self, x: int, y: int) -> tuple[Widget, Widget, Widget] | None:
        """(container, before, after) when (x, y) is on the border between two panels."""
        for c in self._split_containers():
            shown = self._panels_shown(c)
            horizontal = self._axis(c) == "horizontal"
            for a, b in zip(shown, shown[1:]):
                ra, rb = a.region, b.region
                if horizontal:
                    if ra.y <= y < ra.bottom and x in (ra.right - 1, rb.x):
                        return c, a, b
                elif ra.x <= x < ra.right and y in (ra.bottom - 1, rb.y):
                    return c, a, b
        return None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1 or self.screen.maximized is not None:
            return
        hit = self._border_at(event.screen_x, event.screen_y)
        if hit is None:
            return
        container, a, b = hit
        sizes = self._current_sizes(container)
        self._set_sizes(container, sizes)
        self._drag = {"container": container, "a": a, "b": b, "sizes": sizes,
                      "x": event.screen_x, "y": event.screen_y}
        self.capture_mouse()
        event.stop()
        event.prevent_default()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        drag = getattr(self, "_drag", None)
        if drag is None:
            return
        c = drag["container"]
        delta = (event.screen_x - drag["x"] if self._axis(c) == "horizontal"
                 else event.screen_y - drag["y"])
        drag["now"] = self._resize_pair(c, drag["a"], drag["b"], delta, drag["sizes"])
        self._set_sizes(c, drag["now"])
        event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        drag = getattr(self, "_drag", None)
        if drag is None:
            return
        self._drag = None
        self.capture_mouse(False)
        if "now" in drag:
            self._save_sizes(drag["container"], drag["now"])
        event.stop()

    def push_screen(self, *args, **kwargs):
        return self.app.push_screen(*args, **kwargs)

    def call_from_thread(self, *args, **kwargs):
        return self.app.call_from_thread(*args, **kwargs)

    def suspend(self):
        return self.app.suspend()

    def bell(self) -> None:
        self.app.bell()

    def exit(self, *args, **kwargs):
        return self.app.exit(*args, **kwargs)

    @property
    def focused(self):
        return self.app.focused

    def activated(self) -> None:
        """Called when the View's tab is shown (also the first time)."""

    def is_shown(self) -> bool:
        """True when this View's tab is the active one (always, outside tabs)."""
        from textual.widgets import TabbedContent, TabPane
        pane = next((a for a in self.ancestors if isinstance(a, TabPane)), None)
        if pane is None:
            return True
        tabs = next((a for a in pane.ancestors if isinstance(a, TabbedContent)), None)
        return tabs is None or tabs.active == pane.id

    def focus_here(self, widget) -> None:
        """Focus `widget`, but only while this View's tab is shown: focusing a
        widget in a hidden tab makes the app switch to that tab. A View focuses
        its widgets again in activated()."""
        if self.is_shown():
            widget.focus()


class ForwardActions:
    """Mixin for widgets inside a View: `action_x` the widget lacks runs on the View.

    Lets bindings say `Binding("e", "edit")` instead of `"app.edit"`.
    """

    def __getattr__(self, name: str):
        if name.startswith("action_"):
            node = self.parent
            while node is not None:
                if isinstance(node, View):
                    return getattr(node, name)
                node = node.parent
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")
