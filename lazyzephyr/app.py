"""The lazyzephyr app: one tab per tool."""

from __future__ import annotations

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Static, TabbedContent, TabPane

from .dts.app import DTS_CSS, DtsView
from .dts.app import AppConfig as DtsConfig
from .twister.app import TWISTER_CSS, TwisterView
from .twister.app import AppConfig as TwisterConfig
from .view import COMMON_CSS, View

TABS = ("tests", "dts")


class LazyZephyrApp(App):
    TITLE = "lazyzephyr"
    CSS = COMMON_CSS + TWISTER_CSS + DTS_CSS + """
    #tabs { height: 1fr; }
    #tabs > ContentSwitcher { height: 1fr; }
    #tabs TabPane { padding: 0; height: 1fr; }
    TwisterView, DtsView { height: 1fr; }
    .tab-error { padding: 1 2; color: $error; }
    """

    BINDINGS = [
        Binding("q", "request_quit", "Quit"),
        Binding("right_square_bracket", "cycle_tab(1)", "Next tab"),
        Binding("left_square_bracket", "cycle_tab(-1)", "Prev tab", show=False),
        Binding("f1", "show_tab('tests')", show=False),
        Binding("f2", "show_tab('dts')", show=False),
    ]

    def __init__(self, twister: TwisterConfig, dts: DtsConfig | None, tab: str = "tests",
                 dts_error: str | None = None):
        super().__init__()
        self.twister_config, self.dts_config = twister, dts
        self.initial_tab = tab if tab in TABS else "tests"
        self.dts_error = dts_error

    def compose(self) -> ComposeResult:
        with TabbedContent(initial=self.initial_tab, id="tabs"):
            with TabPane("Tests", id="tests"):
                yield TwisterView(self.twister_config, id="twister")
            with TabPane("Devicetree", id="dts"):
                if self.dts_config is not None:
                    yield DtsView(self.dts_config, id="devicetree")
                else:
                    yield Static(f"Devicetree unavailable: {self.dts_error}",
                                 classes="tab-error")
        yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self._activate, self.initial_tab)

    @property
    def tabs(self) -> TabbedContent:
        return self.query_one("#tabs", TabbedContent)

    @property
    def twister(self) -> TwisterView:
        return self.query_one(TwisterView)

    def view(self, tab: str) -> View | None:
        views = self.query_one(f"#{tab}", TabPane).query(View)
        return views.first() if views else None

    def _activate(self, tab: str) -> None:
        view = self.view(tab)
        if view is not None:
            view.activated()

    @on(TabbedContent.TabActivated, "#tabs")
    def _tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self._activate(event.pane.id)

    def action_show_tab(self, tab: str) -> None:
        self.tabs.active = tab

    def action_cycle_tab(self, step: int) -> None:
        index = TABS.index(self.tabs.active) if self.tabs.active in TABS else 0
        self.tabs.active = TABS[(index + step) % len(TABS)]

    def action_request_quit(self) -> None:
        # Asks first while twister jobs are running, whichever tab is shown.
        self.twister.action_request_quit()
