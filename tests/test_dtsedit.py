import textwrap

from lazydts import dtsedit

LABELS = {"i2c0": "/soc/i2c@100", "gpio0": "/soc/gpio@200", "uart0": "/soc/uart@300"}

SRC = textwrap.dedent("""\
    /dts-v1/;
    #include <foo.h>
    #define X(a) \\
    	(a)

    / {
    	model = "test";  // a comment { not a block
    	chosen {
    		zephyr,console = &uart0;
    	};
    };

    &i2c0 {
    	status = "okay";
    	clock-frequency = <I2C_BITRATE_FAST>;

    	sensor: sensor@76 {
    		compatible = "bosch,bme280";
    		reg = <0x76>;
    	};
    };

    #ifdef FOO
    &gpio0 { status = "okay"; };
    #else
    &gpio0 { status = "disabled"; };
    #endif

    &{/soc/uart@300} {
    	/delete-property/ current-speed;
    };
    """)


def resolve(label):
    return LABELS.get(label)


def scan(text=SRC):
    return dtsedit.scan(text, resolve)


def test_blocks_resolve_to_paths():
    sf = scan()
    paths = [(b.path, b.line) for b in sf.blocks]
    assert ("/", 6) in paths
    assert ("/chosen", 8) in paths
    assert ("/soc/i2c@100", 13) in paths
    assert ("/soc/i2c@100/sensor@76", 17) in paths
    assert ("/soc/uart@300", 29) in paths
    # Only the first #if branch is scanned.
    assert [b.line for b in sf.blocks if b.path == "/soc/gpio@200"] == [24]


def test_statements():
    sf = scan()
    [freq] = sf.props_for("/soc/i2c@100", "clock-frequency")
    assert freq.value == "<I2C_BITRATE_FAST>" and freq.line == 15
    [model] = sf.props_for("/", "model")
    assert model.value == '"test"'
    [deleted] = sf.props_for("/soc/uart@300", "current-speed")
    assert deleted.kind == "delete-property"
    sensor = sf.blocks_for("/soc/i2c@100/sensor@76")[0]
    assert sensor.labels == ["sensor"] and sensor.by_name


def test_set_existing_property_in_place():
    sf = scan()
    new = dtsedit.set_property(sf, "/soc/i2c@100", ["i2c0"], "clock-frequency", "<100000>")
    assert "\tclock-frequency = <100000>;\n" in new
    assert new.count("clock-frequency") == 1


def test_set_new_property_in_existing_block():
    sf = scan()
    new = dtsedit.set_property(sf, "/soc/i2c@100/sensor@76", [], "foo", "<1>")
    assert '\t\treg = <0x76>;\n\t\tfoo = <1>;\n\t};' in new


def test_set_property_appends_override_block():
    sf = scan()
    new = dtsedit.set_property(sf, "/soc/spi@400", ["spi0"], "status", '"okay"')
    assert new.endswith('\n\n&spi0 {\n\tstatus = "okay";\n};\n')
    new = dtsedit.set_property(sf, "/soc/spi@400", [], "status", '"okay"')
    assert new.endswith('&{/soc/spi@400} {\n\tstatus = "okay";\n};\n')


def test_boolean_property():
    sf = scan()
    new = dtsedit.set_property(sf, "/soc/i2c@100", ["i2c0"], "wakeup-source", None)
    assert "\twakeup-source;\n" in new


def test_set_after_delete_property():
    sf = scan()
    new = dtsedit.set_property(sf, "/soc/uart@300", [], "current-speed", "<9600>")
    assert "/delete-property/ current-speed;\n\tcurrent-speed = <9600>;" in new


def test_single_line_block():
    sf = dtsedit.scan("&i2c0 { status = \"okay\"; };\n", resolve)
    new = dtsedit.set_property(sf, "/soc/i2c@100", ["i2c0"], "foo", "<1>")
    assert new == '&i2c0 { status = "okay";\n\tfoo = <1>;\n};\n'


def test_remove_statement_and_block():
    sf = scan()
    [status] = sf.props_for("/soc/i2c@100", "status")
    new = dtsedit.remove_statement(sf, status)
    assert 'status = "okay";\n\tclock' not in new and "&i2c0 {\n\tclock-frequency" in new
    block = sf.blocks_for("/soc/i2c@100/sensor@76")[0]
    new = dtsedit.remove_block(sf, block)
    assert "sensor" not in new
    assert "clock-frequency = <I2C_BITRATE_FAST>;\n\n};" in new or \
        "clock-frequency = <I2C_BITRATE_FAST>;\n};" in new


def test_add_node_into_existing_block():
    sf = scan()
    lines = dtsedit.node_lines("widget@10", "widget", [("compatible", '"vnd,widget"'),
                                                      ("reg", "<0x10>")])
    new = dtsedit.add_node(sf, "/soc/i2c@100", ["i2c0"], lines)
    assert ('\t};\n\n\twidget: widget@10 {\n\t\tcompatible = "vnd,widget";\n'
            '\t\treg = <0x10>;\n\t};\n};') in new


def test_add_node_new_parent_block():
    sf = dtsedit.scan("", resolve)
    lines = dtsedit.node_lines("widget@10", None, [("compatible", '"vnd,widget"')])
    new = dtsedit.add_node(sf, "/soc/i2c@100", ["i2c0"], lines)
    assert new == '&i2c0 {\n\twidget@10 {\n\t\tcompatible = "vnd,widget";\n\t};\n};\n'


def test_macro_child_names_use_callback():
    text = "/ {\n\titcm: itcm@ITCM_BASE {\n\t\treg = <1>;\n\t};\n};\n"
    sf = dtsedit.scan(text, resolve, lambda parent, name, labels: "/itcm@0")
    assert sf.blocks_for("/itcm@0")


def test_nested_include_root():
    sf = dtsedit.scan("uart@1 { status = \"okay\"; };\n", resolve, root="/soc/peripheral@5")
    assert sf.props_for("/soc/peripheral@5/uart@1", "status")
    outer = dtsedit.scan('/ {\n\tsoc {\n#include "inner.dtsi"\n\t};\n};\n', resolve)
    assert outer.nested_includes == [("inner.dtsi", "/soc")]
