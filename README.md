# lazyzephyr

A lazygit-style terminal UI for Zephyr work in a **west** workspace, with one tab per tool:

- **Build**: saved build configs to build, flash, run a serial monitor, menuconfig and
  more with a keypress.
- **Devicetree**: browse a target's devicetree and edit it, limited to what the bindings
  allow.
- **Kconfig**: a build's options: why each has its value, what it depends on, and
  changing them in your `prj.conf`.
- **Tests**: run **twister** tests from a live tree of scenarios, with a job queue and
  results that jump to the failing assert.

Zephyr only; not Linux devicetrees.

> **Disclaimer:** lazyzephyr is a side project, built mainly to find out what I can make
> with [Claude Code](https://claude.com/claude-code). Nearly all of the code was written by
> Claude, with me steering, testing and deciding what to build. It works well for me, but
> treat it as an experiment rather than a polished, supported tool.

## Install

### With pipx or uv

lazyzephyr is on [PyPI](https://pypi.org/project/lazyzephyr/). It needs Python 3.11 or
newer. Install it as a tool, in its own environment:

```sh
pipx install lazyzephyr         # or: uv tool install lazyzephyr
pipx upgrade lazyzephyr         # or: uv tool upgrade lazyzephyr
```

For the latest `main` instead of the last release:

```sh
pipx install git+https://github.com/hansbinderup/lazyzephyr   # or: uv tool install git+…
```

`pip install lazyzephyr` works too, e.g. into the Python environment that runs `west`
(needed for `west lazyzephyr`, see below).

### Install script (any Linux)

```sh
curl -fsSL https://raw.githubusercontent.com/hansbinderup/lazyzephyr/main/install.sh | bash
```

This needs Python 3.11 or newer with the `venv` module (on Debian/Ubuntu:
`sudo apt install python3-venv`). No root is needed. The script:

- creates a virtualenv in `~/.local/share/lazyzephyr` and installs lazyzephyr and its
  dependencies (textual, watchfiles, PyYAML, pyte) into it
- links the `lazyzephyr` command into `~/.local/bin` (and tells you if that isn't on
  your `PATH`)

Run it again to update; if an update fails, the current install is left as it was.
To install a branch, tag or commit, set `LAZYZEPHYR_REF`:

```sh
curl -fsSL https://raw.githubusercontent.com/hansbinderup/lazyzephyr/main/install.sh | LAZYZEPHYR_REF=v0.1.0 bash
```

Other overrides: `PYTHON` (interpreter to use), `LAZYZEPHYR_HOME` (install directory) and
`LAZYZEPHYR_BIN_DIR` (where the command is linked).

Or run it from a clone, which installs from the clone (handy for local changes):

```sh
git clone https://github.com/hansbinderup/lazyzephyr.git && ./lazyzephyr/install.sh
```

To uninstall:

```sh
curl -fsSL https://raw.githubusercontent.com/hansbinderup/lazyzephyr/main/uninstall.sh | bash
# or from a clone: ./uninstall.sh
```

This removes the virtualenv and the `~/.local/bin/lazyzephyr` link (only if it points
at that virtualenv). What lazyzephyr keeps in `~/.cache/lazyzephyr` (build configs, test
results, layouts) stays; add `--purge` to remove it too (`… | bash -s -- --purge`, or
`./uninstall.sh --purge`).

### With Nix

The repo is a flake (needs flakes enabled: `experimental-features = nix-command flakes`).

Try it without installing:

```sh
nix run github:hansbinderup/lazyzephyr
```

Install it into your profile, and upgrade later:

```sh
nix profile install github:hansbinderup/lazyzephyr   # Nix >= 2.27 also accepts: nix profile add
nix profile upgrade lazyzephyr
```

From a local checkout, use `.` (or the path to it) instead of `github:hansbinderup/lazyzephyr`.
Nix only sees files tracked by git, so `git add` new files before building.

On NixOS or with home-manager, add it as a flake input:

```nix
# flake.nix
inputs.lazyzephyr.url = "github:hansbinderup/lazyzephyr";

# configuration.nix (NixOS)             or home.nix (home-manager)
environment.systemPackages = [          # home.packages = [
  inputs.lazyzephyr.packages.${pkgs.stdenv.hostPlatform.system}.default
];
```

Pass `inputs` to your modules with `specialArgs` (NixOS) or `extraSpecialArgs` (home-manager).
To pin a branch, tag or commit: `github:hansbinderup/lazyzephyr/<ref>`.

Inside a workspace that has its own flake, you can also add lazyzephyr to that flake's dev
shell (`packages = [ inputs.lazyzephyr.packages.${system}.default ];`), so it's on `PATH`
together with `west` and the SDK.

## Running

Start it **from inside the workspace's dev shell**, so that `west`, the Zephyr SDK and a C
compiler are on `PATH`:

```sh
cd ~/zephyrproject && nix develop
lazyzephyr                                  # first time: asks for test folders / target
lazyzephyr -T tests/lib -p native_sim/native/64 -j 8 -- --coverage
lazyzephyr -d build                         # Devicetree tab on an existing build
lazyzephyr -b native_sim/native/64 -a app   # Devicetree tab on a board + app overlays
```

Arguments after `--` are passed to every twister invocation. `lazyzephyr --help` lists
all options.

### As `west lazyzephyr`

West extension commands must come from a project in the manifest. Add lazyzephyr to
your `west.yml`:

```yaml
    - name: lazyzephyr
      path: tools/lazyzephyr
      url: https://github.com/hansbinderup/lazyzephyr
      west-commands: west-commands.yml
```

Its Python dependencies must also be installed in west's environment
(`pip install lazyzephyr` there does it).

## Documentation

How to use each tab, and every key:
**[docs/](https://github.com/hansbinderup/lazyzephyr/blob/main/docs/README.md)**
([Build](https://github.com/hansbinderup/lazyzephyr/blob/main/docs/build.md),
[Devicetree](https://github.com/hansbinderup/lazyzephyr/blob/main/docs/devicetree.md),
[Kconfig](https://github.com/hansbinderup/lazyzephyr/blob/main/docs/kconfig.md),
[Tests](https://github.com/hansbinderup/lazyzephyr/blob/main/docs/tests.md)). In the app,
the footer shows the tab's keys and `?` its help.

## Development

```sh
uv venv .venv && uv pip install -e '.[dev]'
.venv/bin/pytest
# or with Nix:
nix develop -c python -m pytest
nix build                       # builds the package and runs the tests; result in ./result
```

The Tests tab's tests use `tests/twister/fake_twister.py` in place of twister. The
Devicetree, Kconfig and Build tests need a Zephyr tree: set `LAZYZEPHYR_TEST_ZEPHYR_BASE`
(or `ZEPHYR_BASE`) to one, else they are skipped.

## License

[Apache-2.0](https://github.com/hansbinderup/lazyzephyr/blob/main/LICENSE)
