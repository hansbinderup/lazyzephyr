import pytest

from lazydts.values import format_value, to_input


@pytest.mark.parametrize("type_,text,expected", [
    ("int", "5", "<5>"),
    ("int", "<0x20>", "<0x20>"),
    ("int", "DT_FREQ_M(32)", "<DT_FREQ_M(32)>"),
    ("array", "1, 2 3", "<1 2 3>"),
    ("uint8-array", "0 1f AB", "[00 1f ab]"),
    ("string", "hello", '"hello"'),
    ("string", '"quoted"', '"quoted"'),
    ("string-array", "a, b", '"a", "b"'),
    ("string-array", '"a", "b"', '"a", "b"'),
    ("phandle", "&gpio0", "<&gpio0>"),
    ("phandles", "dma0 &dma1", "<&dma0 &dma1>"),
    ("phandle-array", "&gpio0 5 GPIO_ACTIVE_LOW", "<&gpio0 5 GPIO_ACTIVE_LOW>"),
    ("phandle-array", "<&gpio0 1 0>, <&gpio1 2 0>", "<&gpio0 1 0>, <&gpio1 2 0>"),
    ("path", "&uart0", "&uart0"),
    ("path", "/soc/uart", '"/soc/uart"'),
])
def test_format(type_, text, expected):
    labels = {"gpio0", "gpio1", "dma0", "dma1", "uart0"}
    assert format_value(type_, text, labels=labels) == expected


@pytest.mark.parametrize("type_,text,enum", [
    ("int", "1 2", None),
    ("int", "3", [1, 2]),
    ("int", "1+", None),
    ("string", "c", ["a", "b"]),
    ("uint8-array", "xyz", None),
    ("phandle", "&nope", None),
    ("phandle-array", "<&nope 1>", None),
    ("path", "relative", None),
])
def test_format_rejects(type_, text, enum):
    with pytest.raises(ValueError):
        format_value(type_, text, enum, labels={"gpio0"})


def test_to_input_round_trip():
    for type_, raw in [("int", "<0x20>"), ("string", '"x"'), ("string-array", '"a", "b"'),
                       ("uint8-array", "[00 01]"), ("phandle", "<&gpio0>")]:
        assert format_value(type_, to_input(type_, raw), labels={"gpio0"}) == raw
