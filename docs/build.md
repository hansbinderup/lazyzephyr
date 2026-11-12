# Build tab

## Features

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
  Devicetree tab (`t`) or the Kconfig tab (`K`). Output streams into the Output panel;
  menuconfig, debug, attach and RTT take over the terminal until they exit.
- **Monitor**: `o` opens a serial monitor for the config in a terminal panel inside the
  tab, running whatever command you give it (asked the first time, then saved with the
  config), e.g. `tio /dev/ttyACM0` or `west espressif monitor -d $BUILD_DIR`. It runs with
  `sh` in the workspace, with `$BUILD_DIR` and `$BOARD` set. While it has focus every key
  goes to it except `ctrl+o` (back to the configs; it keeps running), `ctrl+z` (full
  screen; `esc` also leaves it) and `ctrl+x` (close the connection). `shift+pgup` /
  `shift+pgdn` or the mouse wheel scroll back. From the configs list, `O` closes it.
- **Runners** come from the build's `runners.yaml` once built, before that from the
  board's `board.cmake`. **Probes** (J-Link, ST-Link, CMSIS-DAP, MCU-Link…) are read from
  USB, so you can pick the serial number of the one to flash.
- The details panel shows the exact `west build` and `west flash` commands.
- The default build directory is `build-<app>-<board>` in the workspace, or west's
  `build.dir-fmt` if you set one.

## Keys

| Key | Action |
| --- | --- |
| `enter` / `space` | all actions for the selected config (imports a found build) |
| `b` / `B` | build / pristine build |
| `f` / `F` | flash / build, then flash |
| `m` / `t` / `K` | menuconfig / open the build in the Devicetree / Kconfig tab |
| `o` / `O` | open (or go back to) the monitor / close it |
| `ctrl+o` / `ctrl+z` / `ctrl+x` | in the monitor: back to the configs / full screen / close |
| `n` / `e` / `c` / `d` | new / edit / copy / delete config |
| `x` | stop the running command |
| `L` / `z` / `?` | clear the output / maximize the focused panel / help |
