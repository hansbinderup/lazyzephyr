import os
import textwrap
from pathlib import Path

import pytest


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"))
    return path


def _zephyr_base() -> Path | None:
    for env in ("LAZYDTS_TEST_ZEPHYR_BASE", "ZEPHYR_BASE"):
        value = os.environ.get(env)
        if value and (Path(value) / "scripts" / "dts" / "python-devicetree").is_dir():
            return Path(value).resolve()
    return None


@pytest.fixture
def zephyr_base() -> Path:
    base = _zephyr_base()
    if base is None:
        pytest.skip("needs a Zephyr tree: set LAZYDTS_TEST_ZEPHYR_BASE or ZEPHYR_BASE")
    return base


@pytest.fixture
def workspace(tmp_path: Path, zephyr_base: Path, monkeypatch) -> Path:
    """A west workspace using the real Zephyr, with an app overlay for native_sim."""
    monkeypatch.delenv("ZEPHYR_BASE", raising=False)
    top = tmp_path / "ws"
    write(top / ".west" / "config", f"""
        [manifest]
        path = app

        [zephyr]
        base = {zephyr_base}
        """)
    write(top / "app" / "CMakeLists.txt", "# app\n")
    write(top / "app" / "boards" / "native_sim.overlay", """
        / {
        	aliases {
        		led0 = &led0;
        	};

        	leds {
        		compatible = "gpio-leds";
        		led0: led_0 {
        			gpios = <&gpio0 0 0>;
        		};
        	};
        };

        &i2c0 {
        	status = "okay";
        };
        """)
    # A module with its own binding.
    write(top / "mod" / "zephyr" / "module.yml", """
        build:
          settings:
            dts_root: .
        """)
    write(top / "mod" / "dts" / "bindings" / "vnd,widget.yaml", """
        description: Test widget on I2C
        compatible: "vnd,widget"
        include: i2c-device.yaml
        properties:
          mode:
            type: string
            enum: [fast, slow]
            required: true
          gain:
            type: int
            default: 1
        """)
    return top
