#!/usr/bin/env bash
# Install lazyzephyr for the current user (no root needed).
#
#   curl -fsSL https://raw.githubusercontent.com/hansbinderup/lazyzephyr/main/install.sh | bash
#
# or from a clone:  ./install.sh
#
# Creates a Python virtualenv in ~/.local/share/lazyzephyr and links the
# `lazyzephyr` command into ~/.local/bin. Run it again to update.
# Needs: Python >= 3.11 with the venv module, and curl or a clone of the repo.
#
# Environment overrides:
#   LAZYZEPHYR_REF      branch, tag or commit to install (default: main)
#   LAZYZEPHYR_SOURCE   pip-installable source: URL, archive or directory
#                        (default: the clone this script is in, else GitHub)
#   LAZYZEPHYR_HOME     install directory (default: ~/.local/share/lazyzephyr)
#   LAZYZEPHYR_BIN_DIR  where the command is linked (default: ~/.local/bin)
#   PYTHON               Python interpreter to use (default: newest python3.x found)

# Everything is inside main(), so a partially downloaded script doesn't run.
main() {
    set -euo pipefail

    local repo="hansbinderup/lazyzephyr"
    local ref="${LAZYZEPHYR_REF:-main}"
    local home="${LAZYZEPHYR_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/lazyzephyr}"
    local bin_dir="${LAZYZEPHYR_BIN_DIR:-$HOME/.local/bin}"

    say() { printf '\033[1m%s\033[0m\n' "$*"; }
    die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

    # Source: explicit, else the clone this script lives in, else GitHub.
    local source="${LAZYZEPHYR_SOURCE:-}"
    if [[ -z "$source" ]]; then
        local here=""
        if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
            here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        fi
        if [[ -n "$here" && -f "$here/pyproject.toml" && -d "$here/lazyzephyr" ]]; then
            source="$here"
        else
            source="https://github.com/$repo/archive/$ref.tar.gz"
        fi
    fi

    # Find Python >= 3.11 (tomllib).
    local python="${PYTHON:-}"
    if [[ -z "$python" ]]; then
        local candidate
        for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
            if command -v "$candidate" >/dev/null 2>&1 &&
                "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
                python="$candidate"
                break
            fi
        done
    fi
    [[ -n "$python" ]] || die "Python 3.11 or newer is required (set PYTHON=/path/to/python3)."
    command -v "$python" >/dev/null 2>&1 || die "Python not found: $python"
    "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
        die "$python is older than Python 3.11."

    say "Installing lazyzephyr from $source"
    say "  using $("$python" --version 2>&1) ($(command -v "$python"))"

    # Install into a fresh venv-<time> and point the `venv` link at it only once
    # it works, so a failed update leaves the current install alone.
    mkdir -p "$home" "$bin_dir"
    local new="$home/venv-$(date +%Y%m%d%H%M%S)"
    if ! "$python" -m venv "$new" >/dev/null; then
        rm -rf "$new"
        die "could not create a virtualenv. On Debian/Ubuntu: sudo apt install python3-venv"
    fi
    if ! "$new/bin/python" -m pip install --quiet --disable-pip-version-check "$source"; then
        rm -rf "$new"
        die "pip install failed (see above). The previous install, if any, is unchanged."
    fi
    local old=""
    [[ -L "$home/venv" ]] && old="$(readlink "$home/venv")"
    [[ -d "$home/venv" && ! -L "$home/venv" ]] && rm -rf "$home/venv"
    ln -sfn "$new" "$home/venv"
    [[ -n "$old" && "$old" != "$new" && "$old" == "$home/venv-"* ]] && rm -rf "$old"

    ln -sf "$home/venv/bin/lazyzephyr" "$bin_dir/lazyzephyr"
    "$bin_dir/lazyzephyr" --help >/dev/null || die "installed, but lazyzephyr does not start."

    say "Installed: $bin_dir/lazyzephyr"
    case ":$PATH:" in
        *":$bin_dir:"*) ;;
        *) printf '\nAdd %s to your PATH, e.g. in ~/.bashrc:\n  export PATH="%s:$PATH"\n' \
               "$bin_dir" "$bin_dir" ;;
    esac
    printf '\nStart it inside your west workspace (with west and the Zephyr SDK on PATH):\n'
    printf '  cd ~/zephyrproject && lazyzephyr\n'
}

main "$@"
