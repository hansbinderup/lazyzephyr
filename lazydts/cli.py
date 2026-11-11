"""Command line entry point (`lazydts` and `west lazydts`)."""

from __future__ import annotations

import argparse
from pathlib import Path

from .state import TargetChoice, load_choice
from .workspace import find_workspace


def build_parser(prog: str = "lazydts") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="lazygit-style TUI to browse and edit a Zephyr target's devicetree.",
        epilog="Without -b or -d, the last target used in this directory is opened, or a "
               "picker the first time.",
    )
    add_arguments(parser)
    return parser


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-b", "--board", help="board target, e.g. native_sim/native/64")
    parser.add_argument("-d", "--build-dir", type=Path,
                        help="existing build directory (exact: uses its build_info.yml)")
    parser.add_argument("-a", "--app", type=Path,
                        help="application directory for its overlays (default: the current "
                             "directory if it has a CMakeLists.txt)")
    parser.add_argument("--shield", action="append", default=[],
                        help="shield to apply (repeatable)")
    parser.add_argument("--pick", action="store_true", help="open the target picker on start")


def choice_from_args(args: argparse.Namespace, cwd: Path) -> TargetChoice | None:
    if args.build_dir:
        build = args.build_dir.resolve()
        if not (build / "build_info.yml").is_file():
            raise SystemExit(f"lazydts: no build_info.yml in {build}")
        return TargetChoice(build_dir=str(build))
    if args.board:
        app = args.app.resolve() if args.app else (
            cwd if (cwd / "CMakeLists.txt").is_file() else None)
        if app and not app.is_dir():
            raise SystemExit(f"lazydts: app directory not found: {app}")
        return TargetChoice(board=args.board, app=str(app) if app else None,
                            shields=list(args.shield))
    return load_choice(cwd)


def run(args: argparse.Namespace) -> None:
    from .app import AppConfig, LazyDtsApp

    cwd = Path.cwd().resolve()
    try:
        ws = find_workspace(cwd)
    except LookupError as exc:
        raise SystemExit(f"lazydts: {exc}. Run it inside a west workspace or set "
                         "ZEPHYR_BASE.") from None
    choice = choice_from_args(args, cwd)
    LazyDtsApp(AppConfig(cwd=cwd, workspace=ws, choice=choice,
                         pick_target=args.pick or choice is None)).run()


def main(argv: list[str] | None = None) -> None:
    run(build_parser().parse_args(argv))


if __name__ == "__main__":
    main()
