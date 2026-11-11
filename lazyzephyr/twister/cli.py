"""Command line entry point (`lazyzephyr` and `west tui`)."""

from __future__ import annotations

import argparse
import shlex
import shutil
from pathlib import Path

from . import cache, folders
from .config import CONFIG_NAME, ConfigError, FileConfig, find_config, load_config
from .discovery import default_roots, find_workspace
from .jobs import RunOptions


def default_state_dir(anchor: Path) -> Path:
    return cache.state_dir_for(anchor)


def default_twister_cmd(zephyr_base: Path | None) -> list[str]:
    if shutil.which("west"):
        return ["west", "twister"]
    if zephyr_base and (zephyr_base / "scripts" / "twister").is_file():
        return ["python3", str(zephyr_base / "scripts" / "twister")]
    return ["west", "twister"]


def build_parser(prog: str = "lazyzephyr") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="lazygit-style TUI for running Zephyr twister tests in a west workspace.",
        epilog=f"Arguments after '--' are passed to every twister invocation. Defaults can be "
               f"set in a {CONFIG_NAME} file in the current directory or any parent up to the "
               "west workspace; command line options override it.",
    )
    add_arguments(parser)
    return parser


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-T", "--testsuite-root", action="append", type=Path, default=[],
                        help="test root to discover/run (repeatable; default: the folders "
                             "chosen in the folder browser, which opens the first time)")
    parser.add_argument("--pick-folders", action="store_true",
                        help="open the folder browser on start to choose the test roots")
    parser.add_argument("-p", "--platform", action="append", default=[],
                        help="twister platform (repeatable; default: twister's defaults)")
    parser.add_argument("--no-integration", action="store_true", default=None,
                        help="don't pass -G (integration mode) to twister")
    parser.add_argument("-q", "--quiet", action="store_true", default=None,
                        help="don't pass -v to twister (disables live per-scenario status)")
    parser.add_argument("-j", "--jobs", type=int, default=None,
                        help="twister -j (default: twister's own default)")
    parser.add_argument("-w", "--watch", action="append", type=Path, default=[],
                        help="extra path to watch for changes (repeatable), e.g. lib/")
    parser.add_argument("--auto-run", action="store_true", default=None,
                        help="start with auto-run on file change enabled")
    parser.add_argument("--twister", default=None,
                        help="twister command (default: 'west twister')")
    parser.add_argument("--state-dir", type=Path, default=None,
                        help="where results and twister output go "
                             "(default: ~/.cache/lazyzephyr/<workspace>)")
    parser.add_argument("--cache-info", action="store_true",
                        help="show how much disk space lazyzephyr's cache uses, then exit")
    parser.add_argument("--clear-cache", nargs="?", const="build", choices=["build", "all"],
                        help="delete twister build output for this workspace (keeps results), "
                             "or with 'all' everything lazyzephyr stored for every "
                             "workspace; then exit")
    config = parser.add_mutually_exclusive_group()
    config.add_argument("--config", type=Path, default=None,
                        help=f"config file to use (default: nearest {CONFIG_NAME})")
    config.add_argument("--no-config", action="store_true",
                        help=f"ignore {CONFIG_NAME} files")


def config_from_args(args: argparse.Namespace, extra: list[str]):
    from .app import AppConfig

    cwd = Path.cwd()
    workspace = find_workspace(cwd)
    file_config = FileConfig()
    if not args.no_config:
        path = args.config or find_config(cwd, workspace.topdir)
        if args.config and not args.config.is_file():
            raise SystemExit(f"lazyzephyr: config file not found: {args.config}")
        if path:
            try:
                file_config = load_config(path)
            except ConfigError as exc:
                raise SystemExit(f"lazyzephyr: {exc}") from None

    def pick(cli_value, file_value, default):
        return cli_value if cli_value is not None else (
            file_value if file_value is not None else default
        )

    state_dir = (args.state_dir or default_state_dir(workspace.topdir or cwd)).resolve()
    saved = folders.load_saved(state_dir, cwd)
    if args.testsuite_root:
        roots, source = [r.resolve() for r in args.testsuite_root], "cli"
    elif file_config.roots:
        roots, source = file_config.roots, "config"
    elif saved:
        roots, source = saved, "saved"
    else:
        roots, source = default_roots(cwd), "default"
    # First time in this directory (and nothing else says where tests are): ask.
    pick_folders = args.pick_folders or source == "default"
    missing = [r for r in roots if not r.is_dir()]
    if missing:
        raise SystemExit(f"lazyzephyr: test root does not exist: {missing[0]}")
    if args.jobs is not None and args.jobs < 1:
        raise SystemExit("lazyzephyr: --jobs must be >= 1")
    twister_str = args.twister or file_config.twister
    twister = shlex.split(twister_str) if twister_str else default_twister_cmd(
        workspace.zephyr_base
    )
    return AppConfig(
        cwd=cwd,
        roots=roots,
        workspace=workspace,
        twister_cmd=twister,
        state_dir=state_dir,
        options=RunOptions(
            platforms=args.platform or file_config.platforms,
            integration=pick(None if args.no_integration is None else False,
                             file_config.integration, True),
            verbose=pick(None if args.quiet is None else False, file_config.verbose, True),
            extra_args=[*file_config.extra_args, *extra],
            jobs=pick(args.jobs, file_config.jobs, None),
        ),
        watch_paths=[w.resolve() for w in args.watch] or file_config.watch,
        auto_run=pick(args.auto_run, file_config.auto_run, False),
        config_file=file_config.path,
        roots_source=source,
        pick_folders=pick_folders,
    )


def split_extra(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        index = argv.index("--")
        return argv[:index], argv[index + 1:]
    return argv, []


def cache_command(args: argparse.Namespace) -> None:
    """--cache-info / --clear-cache: work on the cache without starting the TUI."""
    workspace = find_workspace(Path.cwd())
    state_dir = (args.state_dir or default_state_dir(workspace.topdir or Path.cwd())).resolve()
    if args.cache_info:
        rows = cache.workspaces()
        if state_dir.parent != cache.cache_root() and (state_dir.is_dir()):
            rows.append((state_dir, *cache.usage(state_dir)))
        if not rows:
            print(f"lazyzephyr cache is empty ({cache.cache_root()})")
            return
        total = 0
        for path, builds, results in rows:
            marker = "*" if path == state_dir else " "
            print(f"{marker} {cache.human(builds):>9} build output  "
                  f"{cache.human(results):>9} results   {path}")
            total += builds + results
        print(f"  {cache.human(total):>9} total   (* = this workspace)")
        return
    if args.clear_cache == "build":
        freed = cache.clear(state_dir)
        print(f"freed {cache.human(freed)} of twister build output in {state_dir}")
    else:
        dirs = {path for path, _, _ in cache.workspaces()} | {state_dir}
        freed = sum(cache.clear(path, include_results=True) for path in sorted(dirs))
        print(f"freed {cache.human(freed)}: build output and results for "
              f"{len(dirs)} workspace(s)")


def run(args: argparse.Namespace, extra: list[str]) -> None:
    if args.cache_info or args.clear_cache:
        cache_command(args)
        return
    from .app import LazyTwisterApp

    LazyTwisterApp(config_from_args(args, extra)).run()


def main(argv: list[str] | None = None) -> None:
    import sys

    own, extra = split_extra(list(sys.argv[1:] if argv is None else argv))
    args = build_parser().parse_args(own)
    run(args, extra)


if __name__ == "__main__":
    main()
