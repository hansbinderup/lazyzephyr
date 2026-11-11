"""Base for a tool shown as one tab of the lazyzephyr app.

Each tool (tests, devicetree) started life as its own Textual App. As a View it
keeps that code mostly as is: the few App-only calls are forwarded to the real
app, and child widgets forward their actions to the View that owns them.
"""

from __future__ import annotations

from textual.containers import Vertical

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
    """A whole tool in one widget."""

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
