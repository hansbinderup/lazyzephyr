"""`west lazyzephyr` extension command.

West loads this file by path, so make the package importable from the
repository checkout even when it is not pip-installed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from west.commands import WestCommand  # noqa: E402

from lazyzephyr.cli import add_arguments, run  # noqa: E402


class LazyZephyrCommand(WestCommand):
    def __init__(self):
        super().__init__(
            "lazyzephyr",
            "lazygit-style TUI for twister tests and the devicetree",
            "Run twister tests and browse/edit the devicetree, one tab each. "
            "Arguments after '--' are passed to every twister invocation.",
            accepts_unknown_args=True,
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        add_arguments(parser)
        return parser

    def do_run(self, args, unknown_args):
        extra = unknown_args[1:] if unknown_args[:1] == ["--"] else unknown_args
        run(args, extra)
