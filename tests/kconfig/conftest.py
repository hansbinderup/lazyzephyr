"""A fake build directory with a small Kconfig tree, read with Zephyr's kconfiglib."""

import sys
from pathlib import Path

import pytest

from ..conftest import write

KCONFIG = """
mainmenu "Test"

menu "Drivers"

config I2C
	bool "I2C drivers"
	help
	  Enable I2C.

config I2C_SHELL
	bool "I2C shell"
	depends on SHELL && I2C

config SENSOR
	bool "Sensors"
	select I2C

config SPEED
	int "Bus speed"
	range 1 1000
	default 100
	depends on I2C

endmenu

config SHELL
	bool "Shell"
	default y

choice LOG_MODE
	prompt "Log mode"
	default LOG_MODE_DEFERRED

config LOG_MODE_DEFERRED
	bool "Deferred"

config LOG_MODE_IMMEDIATE
	bool "Immediate"

endchoice

config NAME
	string "Device name"
	default "dev"

config HIDDEN_HELPER
	bool
"""


@pytest.fixture
def kbuild(workspace: Path, zephyr_base: Path) -> Path:
    """`workspace`/app/build: a build of a tiny Kconfig tree, prj.conf has a line
    that has no effect (I2C_SHELL needs I2C)."""
    top = workspace
    kroot = write(top / "kconf" / "Kconfig", KCONFIG)
    build = top / "app" / "build"
    (build / "zephyr").mkdir(parents=True)
    defconfig = write(top / "boards" / "test_defconfig", 'CONFIG_NAME="board"\n')
    prj = write(top / "app" / "prj.conf", "CONFIG_I2C_SHELL=y\n")
    dot = build / "zephyr" / ".config"
    write(build / "build.ninja", f"""
        build menuconfig: phony CMakeFiles/menuconfig
        build CMakeFiles/menuconfig | ${{cmake_ninja_workdir}}CMakeFiles/menuconfig: CUSTOM_COMMAND
          COMMAND = cd {build}/zephyr/kconfig && /usr/bin/cmake -E env ZEPHYR_BASE={zephyr_base} srctree={top / 'kconf'} KCONFIG_CONFIG={dot} BOARD=test {sys.executable} {zephyr_base}/scripts/kconfig/menuconfig.py {kroot}
        """)
    write(build / "build_info.yml", f"""
        cmake:
          board:
            name: test_board
          kconfig:
            files:
             - '{defconfig}'
             - '{prj}'
            user-files:
             - '{prj}'
        """)
    # The last build's .config: what the conf files give.
    from lazyzephyr.kconfig import loader
    model = loader.load(loader.find(build))
    model.kconf.write_config(str(dot))
    return build
