from pathlib import Path

from lazyzephyr.dts import macros, specifiers
from lazyzephyr.dts.specifiers import Controller

from ..conftest import write

GPIO = Controller("&gpio0", "/soc/gpio@0", ["pin", "flags"], "vnd,gpio", limit=32,
                  refs={"&gpio0", "&{/soc/gpio@0}"})
PWM = Controller("&pwm0", "/soc/pwm@0", ["channel", "period", "flags"], "vnd,pwm",
                 refs={"&pwm0", "&my_pwm"})


def test_parse_groups_and_runs():
    assert specifiers.parse("<&gpio0 5 GPIO_ACTIVE_LOW>", [GPIO]) == \
        [("&gpio0", ["5", "GPIO_ACTIVE_LOW"])]
    # Several entries in one <...>, and other ways to name the controller.
    assert specifiers.parse("<&gpio0 1 0 &my_pwm 2 PWM_MSEC(20) (A | B)>", [GPIO, PWM]) == \
        [("&gpio0", ["1", "0"]), ("&pwm0", ["2", "PWM_MSEC(20)", "(A | B)"])]
    assert specifiers.parse("<&gpio0 1 0>, <&{/soc/gpio@0} 2 0>", [GPIO]) == \
        [("&gpio0", ["1", "0"]), ("&gpio0", ["2", "0"])]
    assert specifiers.parse(None, [GPIO]) == []


def test_parse_gives_up_on_what_it_cant_split():
    assert specifiers.parse("<&gpio0 1>", [GPIO]) is None  # too few cells
    assert specifiers.parse("<&unknown 1 2>", [GPIO]) is None
    assert specifiers.parse("MY_GPIOS", [GPIO]) is None


def test_compose_wraps_expressions():
    assert specifiers.compose([("&gpio0", ["5", "GPIO_ACTIVE_LOW | GPIO_PULL_UP"]),
                               ("&pwm0", ["1", "PWM_MSEC(20)", "(A | B)"])]) == \
        "<&gpio0 5 (GPIO_ACTIVE_LOW | GPIO_PULL_UP)>, <&pwm0 1 PWM_MSEC(20) (A | B)>"
    assert specifiers.cell_text("-1") == "(-1)"


def test_check():
    assert specifiers.check([("&gpio0", ["3", "0"])], [GPIO]) is None
    assert "pin 32 is out of range" in specifiers.check([("&gpio0", ["32", "0"])], [GPIO])
    assert specifiers.check([("&gpio0", ["3", ""])], [GPIO]) == "flags is empty"
    assert specifiers.check([("", [])], [GPIO]) == "pick a controller"
    assert specifiers.check([], [GPIO]).startswith("add at least one entry")


def test_space():
    class Spec:
        specifier_space = None
    assert specifiers.space("reset-gpios", Spec) == "gpio"
    assert specifiers.space("io-channels", Spec) == "io-channel"
    Spec.specifier_space = "foo"
    assert specifiers.space("bars", Spec) == "foo"


def test_macros_scan_and_rank(tmp_path: Path):
    gpio = write(tmp_path / "dt-bindings" / "gpio" / "gpio.h", """
        #ifndef ZEPHYR_INCLUDE_DT_BINDINGS_GPIO_GPIO_H_
        #define ZEPHYR_INCLUDE_DT_BINDINGS_GPIO_GPIO_H_
        #define GPIO_ACTIVE_LOW (1 << 0) /* low */
        #define GPIO_PULL_UP \\
                (1 << 4)
        #endif
        """)
    i2c = write(tmp_path / "dt-bindings" / "i2c" / "i2c.h", """
        #define I2C_BITRATE_FAST 400000
        #define I2C_ADDR(x) (x)
        """)
    found = macros.scan([gpio, i2c])
    assert [(m.name, m.value) for m in found] == [
        ("GPIO_ACTIVE_LOW", "(1 << 0)"), ("GPIO_PULL_UP", "(1 << 4)"),
        ("I2C_BITRATE_FAST", "400000"), ("I2C_ADDR", "(x)")]
    assert found[3].insert == "I2C_ADDR("
    # No token: only what fits the context.
    assert [m.name for m in macros.rank(found, "", {"i2c"})] == ["I2C_BITRATE_FAST", "I2C_ADDR"]
    # A token searches everything, prefix matches first.
    assert [m.name for m in macros.rank(found, "up", {"i2c"})] == ["GPIO_PULL_UP"]
    assert [m.name for m in macros.rank(found, "i2c_b", set())] == ["I2C_BITRATE_FAST"]
    assert macros.context_words("nordic,nrf-saadc", "io-channel") == \
        {"nordic", "nrf", "saadc", "channel"}


def test_zero_cell_controller():
    osc = Controller("&osc", "/osc", [], refs={"&osc"})
    assert specifiers.parse("<&osc>, <&osc>", [osc]) == [("&osc", []), ("&osc", [])]
    assert specifiers.compose([("&osc", [])]) == "<&osc>"
    assert specifiers.check([("&osc", [])], [osc]) is None
