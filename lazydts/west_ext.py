"""`west lazydts` extension command.

West loads this file by path, so make the package importable from the
repository checkout even when it is not pip-installed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from west.commands import WestCommand  # noqa: E402

from lazydts.cli import add_arguments, run  # noqa: E402


class LazyDtsCommand(WestCommand):
    def __init__(self):
        super().__init__(
            "lazydts",
            "lazygit-style TUI for the devicetree",
            "Browse a target's devicetree and edit it within what the bindings allow.",
        )

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(self.name, help=self.help, description=self.description)
        add_arguments(parser)
        return parser

    def do_run(self, args, unknown_args):
        run(args)
