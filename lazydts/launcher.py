"""Entry point that isolates lazydts from a foreign PYTHONPATH.

Zephyr/west dev shells (e.g. `nix develop`) export a PYTHONPATH pointing at
*their* interpreter's site-packages. When lazydts runs from its own
environment, those entries can shadow its dependencies and break imports.

This module must only import the standard library.
"""

import os
import sys


def isolate_sys_path() -> None:
    pythonpath = os.environ.get("PYTHONPATH")
    if not pythonpath or os.environ.get("LAZYDTS_KEEP_PYTHONPATH"):
        return
    foreign = {os.path.realpath(p) for p in pythonpath.split(os.pathsep) if p}
    sys.path[:] = [p for p in sys.path if os.path.realpath(p or os.curdir) not in foreign]


def main() -> None:
    isolate_sys_path()
    from lazydts.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
