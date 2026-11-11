"""References and problem checks against native_sim with a broken overlay."""

import asyncio

from lazyzephyr.dts import checks
from lazyzephyr.dts.app import AppConfig, LazyDtsApp, ProblemsScreen
from lazyzephyr.dts.state import TargetChoice
from lazyzephyr.dts.workspace import find_workspace

from .test_session import make

BROKEN = """
/ {
	chosen {
		zephyr,test-uart = &uart1;
	};

	leds {
		led1: led_1 {
			gpios = <&gpio0 0 0>;
		};
	};
};

&i2c0 {
	first: widget@10 {
		compatible = "vnd,widget";
		reg = <0x10>;
		mode = "fast";
	};

	second: thing@10 {
		compatible = "vnd,widget";
		reg = <0x10>;
		mode = "slow";
	};
};

&flash0 {
	partitions {
		bad_partition: partition@d000 {
			label = "bad";
			reg = <0xd000 0x1000>;
		};
	};
};

&sdl_dc {
	status = "disabled";
};
"""


def broken(workspace):
    overlay = workspace / "app" / "boards" / "native_sim.overlay"
    overlay.write_text(overlay.read_text() + BROKEN)


def test_references(workspace, tmp_path):
    s = make(workspace, tmp_path)
    refs = s.model.references
    gpio = s.model.label_path("gpio0")
    assert checks.Ref("/leds/led_0", "gpios", "pin=0 flags=0") in refs[gpio]
    assert any(r.source == "/aliases" and r.prop == "led0" for r in refs["/leds/led_0"])
    assert s.model.problems == []


def test_problems(workspace, tmp_path):
    broken(workspace)
    s = make(workspace, tmp_path)
    by_kind = {}
    for p in s.model.problems:
        by_kind.setdefault(p.kind, []).append(p)
    i2c = s.model.label_path("i2c0")

    [pin] = by_kind["pin"]
    assert pin.severity == "warning" and "&led0 (gpios)" in pin.message \
        and "&led1 (gpios)" in pin.message

    [address] = by_kind["address"]
    assert address.severity == "error"
    assert set(address.paths) == {f"{i2c}/widget@10", f"{i2c}/thing@10"}
    assert "0x10 on &i2c0" in address.message

    [part] = by_kind["partition"]
    assert "&slot0_partition" in part.message and "&bad_partition" in part.message

    disabled = {p.message for p in by_kind["disabled"]}
    assert "chosen zephyr,test-uart is &uart1, which is disabled" in disabled
    assert "&input_sdl_touch display needs &sdl_dc, which is disabled" in disabled
    # Errors come first.
    assert [p.severity for p in s.model.problems] == \
        sorted((p.severity for p in s.model.problems), key=lambda x: x != "error")


def test_pin_decoders():
    from pathlib import Path
    from lazyzephyr.dts.macros import Macro
    h = Path("x.h")
    nrf, = checks.pin_decoders([Macro("NRF_PIN_MSK", "0x1FFU", h)])
    assert nrf.prop == "psels"
    assert nrf.decode((2 << 24) | (1 * 32 + 3)) == "P1.03"
    assert nrf.decode(0x1FF) is None  # disconnected
    stm, = checks.pin_decoders([Macro("STM32_PORT_SHIFT", "9U", h),
                                Macro("STM32_LINE_SHIFT", "5U", h)])
    assert stm.decode((0 << 9) | (9 << 5) | 7) == "PA9"
    amb, = checks.pin_decoders([Macro("APOLLO4_PIN_NUM_POS", "4", h),
                                Macro("APOLLO4_PIN_NUM_MASK", "0x7f", h)])
    assert amb.decode((30 << 4) | 4) == "P30"


async def wait_for(predicate, timeout=10.0):
    for _ in range(int(timeout / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met")


async def test_problems_screen(workspace, tmp_path, monkeypatch):
    broken(workspace)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ws = find_workspace(workspace)
    app = LazyDtsApp(AppConfig(cwd=workspace / "app", workspace=ws,
                               choice=TargetChoice(board="native_sim/native",
                                                   app=str(workspace / "app")),
                               work_dir=tmp_path / "work"))
    async with app.run_test(size=(180, 50)) as pilot:
        await wait_for(lambda: "/leds/led_1" in app.node_index)
        assert "problems, p: list" in str(app.query_one("#statusbar").render())
        assert app.problem_marks["/leds/led_1"] == "warning"
        await pilot.press("p")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ProblemsScreen)
        lst = screen.query_one("#problems-list")
        target = next(i for i, p in enumerate(screen.problems) if p.kind == "disabled"
                      and p.prop == "display")
        lst.focus()
        lst.highlighted = target
        await pilot.press("enter")
        await wait_for(lambda: app.current_path == "/input-sdl-touch")
        await wait_for(lambda: app.focused is app.query_one("#props"))
        props = app.query_one("#props")
        assert props.get_option_at_index(props.highlighted).id == "display"
        details = str(app.query_one("#details").render())
        assert "needs &sdl_dc" in details
