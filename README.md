# lazyzephyr

A lazygit-style terminal UI for Zephyr work in a **west** workspace, with one tab per tool:

- **Build**: saved build configs (app, board, revision, shields, snippets, sysbuild,
  runner, probe…) to build, flash, menuconfig and more with a keypress.
- **Devicetree**: browse a target's devicetree and edit it, limited to what the bindings
  allow, with a searchable menu to add any node.
- **Tests**: run **twister** tests: see every scenario, queue runs with a keypress, keep
  the results and jump to the assert that failed. (This was lazytwister.)

Zephyr only; not Linux devicetrees.

> **Disclaimer:** lazyzephyr is a side project, built mainly to find out what I can make
> with [Claude Code](https://claude.com/claude-code). Nearly all of the code was written by
> Claude, with me steering, testing and deciding what to build. It works well for me, but
> treat it as an experiment rather than a polished, supported tool.

## Tabs

| Key | Action |
| --- | --- |
| `]` / `[` | next / previous tab |
| `F1` / `F2` / `F3` | Build / Devicetree / Tests |
| `q` | quit (asks first if a twister job is running or queued) |

Every other key belongs to the tab you are in, and the footer shows them. It starts in
the Build tab; `--tab dts` (or giving `-b` / `-d`) starts in Devicetree, `--tab tests` in
Tests.

## Build tab

### Features

- **Saved configs**: an app, a board target (searchable list of every board in the
  workspace) and its revision, shields and snippets (picked from the ones that exist),
  sysbuild on/off, a build directory, extra Kconfig files and overlays, more CMake
  arguments, the flash runner and the probe's serial number. Configs are saved per
  workspace in `~/.cache/lazyzephyr/builds.json`, and the list shows how each last run went.
- **Editing a config** is a list of its settings: move with `j`/`k` or the arrows without
  changing anything, `enter` changes the setting (a searchable list for boards, apps,
  shields, snippets, revisions, runners and probes; a text box otherwise), `e` types the
  value instead, `ctrl+s` saves and `esc` leaves (asking first if something changed).
- **Existing builds** show up under "found builds": `enter` imports one as a config,
  using the `west build` command it was made with (also for sysbuild).
- **Actions**: build, pristine build, flash, build then flash, flash with `--erase`, run
  (native_sim, qemu), menuconfig, guiconfig, hardenconfig, RAM / ROM reports, debug,
  attach, debugserver, RTT, delete the build directory, and open the build in the
  Devicetree tab. Output streams into the Output panel; menuconfig, debug, attach and RTT
  take over the terminal until they exit.
- **Runners** come from the build's `runners.yaml` once built, before that from the
  board's `board.cmake`. **Probes** (J-Link, ST-Link, CMSIS-DAP, MCU-Link…) are read from
  USB, so you can pick the serial number of the one to flash.
- The details panel shows the exact `west build` and `west flash` commands.
- The default build directory is `build-<app>-<board>` in the workspace, or west's
  `build.dir-fmt` if you set one.

### Keys

| Key | Action |
| --- | --- |
| `enter` / `space` | all actions for the selected config (imports a found build) |
| `b` / `B` | build / pristine build |
| `f` / `F` | flash / build, then flash |
| `m` / `t` | menuconfig / open the build in the Devicetree tab |
| `n` / `e` / `c` / `d` | new / edit / copy / delete config |
| `x` | stop the running command |
| `L` / `z` / `?` | clear the output / maximize the focused panel / help |

## Devicetree tab

### Features

- **Pick a target**: an existing build directory (exact: it uses the build's
  `build_info.yml`), or any board in the workspace, with your app's overlays and shields.
  Press `t` to switch. The last target is remembered per directory.
- **See the final tree**: labels, status, compatible, the binding, the bus, and whether a
  driver in the workspace uses the compatible. Details list every file and line that
  defines or overrides the node, across `.dts`, `.dtsi` and overlays.
- **Edit only what the bindings allow**: the properties panel lists the binding's
  properties, with their values and where each is set. Values are checked by type, enum
  and label, then the whole tree is parsed again with Zephyr's own `edtlib`. If it no
  longer parses, the file is put back and the error is shown.
- **`gpios`, `pwms`, `io-channels` and other phandle-arrays** are edited as rows: pick the
  controller from the nodes that can serve it, then fill in its named cells (`pin`,
  `flags`, `channel`…). Pins are checked against the controller's `ngpios`. `ctrl+n` /
  `ctrl+r` add / remove an entry, `ctrl+t` switches to plain text and back.
- **Macro suggestions**: while you type a cell or a number, the macros from the
  `dt-bindings` headers the tree includes are listed (`GPIO_ACTIVE_LOW`,
  `I2C_BITRATE_FAST`…), the ones that fit the property first. `↓` moves into the list,
  `enter` puts the macro in, so `GPIO_ACTIVE_LOW | GPIO_PULL_UP` takes a few keys.
- **Add a node** (`a`): a searchable list of every binding, limited to the ones that fit
  the selected node (e.g. I2C devices under an I2C bus). `✓` marks bindings a driver uses.
  It then asks for `reg` (the `@unit-address` comes from it), a label, the required
  properties and the file to write to.
- **Edits go to the right place**: into the node's existing block in that file, or a new
  `&label { };` block. `w` picks the file. Files in Zephyr itself and in third-party
  modules are never edited. If a later file overrides your change, you are told.
- Edits are written straight to your source files: `u` undoes the last one, and
  `git diff` shows them all. The tree reloads by itself when the files change.

### Keys

| Key | Action |
| --- | --- |
| `/` | filter by name, label or compatible |
| `a` | add a node under the selected node |
| `s` | toggle status okay / disabled |
| `d` | delete the node, or in Properties remove the value |
| `enter` / `e` | in Properties: edit the value |
| `o` | open `$EDITOR` where the node or value is set |
| `w` / `t` | pick the file edits go to / pick another target |
| `u` / `r` | undo the last edit / reload |
| `z` / `?` | maximise the focused panel / help |

A C preprocessor (`gcc` or `clang`) must be on `PATH`, as for any Zephyr build.

## Tests tab

### Features

- **Discovery**: finds every `testcase.yaml` / `sample.yaml` scenario under the test
  roots and every `ZTEST*()` case in their sources, shown as a folder → scenario → case tree.
  Folders show `passed/failed/not run` counts in green/red/blue; "not run" covers
  skipped, filtered, build-only, queued and never-run scenarios.
  Big roots like `zephyr/tests` stay responsive: tests load in the background with a
  spinner, large trees start with only the roots open, and a folder's contents are only
  built when you expand it.
- **Choose the test folders**: the first time you start lazyzephyr in a directory, a
  folder browser opens so you can pick which folders to look for tests in. Each folder
  shows how many `testcase.yaml` / `sample.yaml` files it holds. The choice is
  remembered for that directory; press `O` to change it from inside the app.
- **Live reload**: the tree updates as soon as tests are added, removed or changed
  (via `watchfiles`). Keep it open next to your editor.
- **Run anything**: a folder (`twister -T`), one scenario (`-s`), or a single ztest case
  (`-- -test=suite::test`, native targets).
- **Job queue**: only one twister job runs at a time, so runs never compete for CPU.
  Twister still parallelises the builds within a job.
  - `enter` adds the node under the cursor to the queue. Mark several nodes with `space`,
    then `r` queues them all as **one** job (a single twister invocation).
  - In the Jobs panel, the running job is listed first, then the queue in execution order,
    then finished jobs. `x` cancels the selected job, queued or running, and `J`/`K`
    move a queued job later or earlier. `ctrl+x` clears the whole queue and stops the
    running job.
  - The next job only starts once the previous twister process has fully exited,
    including after a cancel.
- **Results** are kept across runs (per scenario and platform), with failing cases and
  log tails shown in the details panel. `F` queues everything that failed, and `.` repeats
  the last run.
- **Jump to the failure**: lazyzephyr reads where each failed assertion happened (file,
  line and message) from the test log, and shows it in Details. Press `e` on a failed case
  or scenario to open `$EDITOR` at the failing `zassert`.
- **Auto-run** (`w`): when a file changes, lazyzephyr reruns the scenarios it belongs to.
  For a change outside a test directory (any path passed with `-w`), it repeats the last run.


### Adaptive layout

The layout follows the terminal size and switches live as you resize or split panes:

- **Landscape** (wide terminals): two columns, as above.
- **Portrait** (fewer than 110 columns, or fewer columns than twice the number of rows):
  the panels are stacked top to bottom, so long scenario names get the full width.
- **Compact** (small terminals): Details is hidden, and `5` opens it as a pop-up.

The status bar along the bottom always shows the workspace, test roots, platforms, `-G`,
auto-run and the pass/fail totals. `2` opens the full status as a pop-up.

`v` overrides the automatic choice, and `z` maximises the focused panel.


### Test folders

The test roots come from the first of these that is set:

1. `-T` on the command line
2. `roots` in `.lazyzephyr.toml`
3. the folders chosen in the folder browser for this directory

If none is set (the first start in a directory), the folder browser opens, with
`./tests` preselected if it exists. `esc` there uses that default for now and asks
again next time. `--pick-folders` opens the browser on start anyway.

In the browser, `space` selects a folder (everything under it is included), `enter`
applies and `esc` cancels. `[✔]` is a chosen folder, `[·]` is inside one, `[-]` contains
one. The choice is saved in the state dir (`folders.json`), not in your repo.

### Per-workspace config

Put a `.lazyzephyr.toml` in your workspace, so running it there needs no flags.
lazyzephyr looks in the current directory and its parents, up to the west workspace
root. Command line options override the file, and `--no-config` ignores it.

```toml
roots = ["tests"]              # relative to this file
platforms = ["native_sim/native/64"]
integration = true             # pass -G
verbose = true                 # pass -v (live per-scenario status)
auto_run = false
watch = ["lib", "subsys"]      # extra paths that trigger auto-run
twister = "west twister"
jobs = 8                       # twister -j
extra_args = ["--coverage"]
```

Unknown keys and wrong types are rejected with a clear error.

> Dev shells usually export a `PYTHONPATH` pointing at their own Python's packages.
> lazyzephyr ignores it for its own imports, so the dev shell's packages can't shadow
> lazyzephyr's dependencies. The variable is still passed on to twister. Set
> `LAZYZEPHYR_KEEP_PYTHONPATH=1` to disable this.


### Keys

| Key | Action |
| --- | --- |
| `1`, `3`–`5`, `tab` | focus a panel |
| `2` | full status pop-up |
| `enter` | queue the node under the cursor |
| `space` / `c` | mark or unmark a node / clear all marks |
| `r` | queue the marked nodes as one job |
| `a` / `F` / `.` | queue all / queue failed / repeat the last run |
| `p` / `g` | choose platforms / toggle integration mode `-G` |
| `O` | choose the test folders (folder browser) |
| `w` | toggle auto-run on file change |
| `/` / `f` | filter / show failed only |
| `e` | open `$EDITOR` at the failing assert, else at the test source or yaml |
| Jobs panel: `x` / `J` `K` | cancel the selected job / move a queued job later or earlier |
| Jobs panel: `r` / `L` | requeue the job / clear finished jobs |
| `ctrl+x` / `ctrl+r` | clear the queue and stop the running job / rediscover tests |
| `z` | maximise / restore the focused panel |
| `v` | layout: auto → landscape → portrait |
| `C` | clear the cache |
| `?` / `q` | help / quit (asks first if a job is running or queued) |


### Where things go

Results and twister output are stored in `~/.cache/lazyzephyr/<workspace>/` (override
with `--state-dir`). Nothing is written into your repo. All jobs share a single `run`
output directory, which lazyzephyr tells twister to delete at the start of each job
(`--clobber-output`). The cache therefore only ever holds the last job's builds, unless
you pass `-n/--no-clean`.

To free up space:

| Where | What it deletes |
| --- | --- |
| `C` in the app | twister build output, optionally the results history too (asks first) |
| `lazyzephyr --cache-info` | nothing; shows the space used per workspace |
| `lazyzephyr --clear-cache` | twister build output for this workspace; results are kept |
| `lazyzephyr --clear-cache all` | everything lazyzephyr stored, for every workspace |

Clearing only removes files lazyzephyr creates (`run*` build folders and `results.json`),
and never follows symlinks.


## Install

### Install script (any Linux)

```sh
curl -fsSL https://raw.githubusercontent.com/hansbinderup/lazyzephyr/main/install.sh | bash
```

This needs Python 3.11 or newer with the `venv` module (on Debian/Ubuntu:
`sudo apt install python3-venv`). No root is needed. The script:

- creates a virtualenv in `~/.local/share/lazyzephyr` and installs lazyzephyr and its
  dependencies (textual, watchfiles, PyYAML) into it
- links the `lazyzephyr` command into `~/.local/bin` (and tells you if that isn't on
  your `PATH`)

Run it again to update; if an update fails, the current install is left as it was. To install a branch, tag or commit, set `LAZYZEPHYR_REF`:

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
at that virtualenv). Your test results,
twister build output and remembered test folders in `~/.cache/lazyzephyr` are kept; add
`--purge` to remove them too (`… | bash -s -- --purge`, or `./uninstall.sh --purge`).

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

### With pip / uv

```sh
uv venv .venv && uv pip install -e '.[dev]'
```

### Running

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
      url: <git remote of this repo>
      west-commands: west-commands.yml
```

Its Python dependencies must also be installed in west's environment.

## Development

```sh
.venv/bin/pytest
# or with Nix:
nix develop -c python -m pytest
nix build                       # builds the package and runs the tests; result in ./result
```

The Tests tab's tests use `tests/twister/fake_twister.py` in place of twister. The
Devicetree tests need Zephyr's devicetree library: set `LAZYZEPHYR_TEST_ZEPHYR_BASE` (or
`ZEPHYR_BASE`) to a Zephyr tree, else they are skipped.
