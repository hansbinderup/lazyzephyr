"""Command line entry point (`lazyzephyr` and `west lazyzephyr`)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .dts.state import TargetChoice, load_choice
from .dts.workspace import find_workspace as find_dts_workspace
from .twister import cli as twister_cli


def build_parser(prog: str = "lazyzephyr") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="lazygit-style TUI for Zephyr: twister tests and the devicetree, "
                    "one tab each.",
        epilog="Arguments after '--' are passed to every twister invocation. Without -b or "
               "-d, the devicetree tab opens the last target used in this directory.",
    )
    add_arguments(parser)
    return parser


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tab", choices=["tests", "dts"],
                        help="tab to start in (default: tests, or dts with -b/-d)")
    twister_cli.add_arguments(parser.add_argument_group("tests tab (twister)"))
    dts = parser.add_argument_group("devicetree tab")
    dts.add_argument("-b", "--board", help="board target, e.g. native_sim/native/64")
    dts.add_argument("-d", "--build-dir", type=Path,
                     help="existing build directory (exact: uses its build_info.yml)")
    dts.add_argument("-a", "--app", type=Path,
                     help="application directory for its overlays (default: the current "
                          "directory if it has a CMakeLists.txt)")
    dts.add_argument("--shield", action="append", default=[],
                     help="shield to apply (repeatable)")
    dts.add_argument("--pick-target", action="store_true",
                     help="open the devicetree target picker on start")


def dts_choice(args: argparse.Namespace, cwd: Path) -> TargetChoice | None:
    if args.build_dir:
        build = args.build_dir.resolve()
        if not (build / "build_info.yml").is_file():
            raise SystemExit(f"lazyzephyr: no build_info.yml in {build}")
        return TargetChoice(build_dir=str(build))
    if args.board:
        app = args.app.resolve() if args.app else (
            cwd if (cwd / "CMakeLists.txt").is_file() else None)
        if app and not app.is_dir():
            raise SystemExit(f"lazyzephyr: app directory not found: {app}")
        return TargetChoice(board=args.board, app=str(app) if app else None,
                            shields=list(args.shield))
    return load_choice(cwd)


def run(args: argparse.Namespace, extra: list[str]) -> None:
    if args.cache_info or args.clear_cache:
        twister_cli.cache_command(args)
        return
    from .app import LazyZephyrApp
    from .dts.app import AppConfig as DtsConfig

    cwd = Path.cwd().resolve()
    twister_config = twister_cli.config_from_args(args, extra)
    dts_config, dts_error = None, None
    try:
        ws = find_dts_workspace(cwd)
        choice = dts_choice(args, cwd)
        dts_config = DtsConfig(cwd=cwd, workspace=ws, choice=choice,
                               pick_target=args.pick_target or choice is None)
    except LookupError as exc:
        dts_error = f"{exc}. Run inside a west workspace or set ZEPHYR_BASE."
    tab = args.tab or ("dts" if args.board or args.build_dir else "tests")
    LazyZephyrApp(twister_config, dts_config, tab, dts_error).run()


def main(argv: list[str] | None = None) -> None:
    own, extra = twister_cli.split_extra(list(sys.argv[1:] if argv is None else argv))
    run(build_parser().parse_args(own), extra)


if __name__ == "__main__":
    main()
