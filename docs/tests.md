# Tests tab

## Features

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

## Adaptive layout

The layout follows the terminal size and switches live as you resize or split panes:

- **Landscape** (wide terminals): two columns, as above.
- **Portrait** (fewer than 110 columns, or fewer columns than twice the number of rows):
  the panels are stacked top to bottom, so long scenario names get the full width.
- **Compact** (small terminals): Details is hidden, and `5` opens it as a pop-up.

The status bar along the bottom always shows the workspace, test roots, platforms, `-G`,
auto-run and the pass/fail totals. `2` opens the full status as a pop-up.

`v` overrides the automatic choice, and `z` maximises the focused panel.

## Test folders

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

## Per-workspace config

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

## Keys

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

## Where things go

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
