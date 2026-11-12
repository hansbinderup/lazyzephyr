# Kconfig tab

## Features

- **Reads an existing build** (build it in the Build tab first; `t` picks another build
  directory, `K` in the Build tab opens one here). Kconfig is loaded with Zephyr's own
  `kconfiglib`, with the environment the build's `menuconfig` target uses, and the
  build's conf files are applied in order, so the values are exactly what the build gets.
- **Browse and search**: the menu tree shows the options you can change (`A` also shows
  hidden ones, whose dependencies aren't met). `/` searches every option by name,
  prompt or help. `✎` marks options set in your conf files, `*` options a rebuild
  changes.
- **Why it has its value**: set in your `prj.conf:12`, set by the board's defconfig,
  turned on by a `select` (and from which option), the default in effect (with its
  file and line), or that a line in your conf file has no effect and why.
- **Dependencies and selects**: what the option depends on, with each dependency shown
  on or off; what it selects and implies; what selects and implies it. The Related list
  jumps to any of them (`enter`), `backspace` comes back.
- **Change options**: `space` switches an option on or off; `enter` picks from a choice
  or takes a number or text (checked against its range). If an option needs others
  first, you are asked to turn those on too. `d` removes it from your conf file so its
  default applies again, `u` undoes.
- **Changes go to your conf files**, never to `.config` (the build regenerates it): the
  file that already sets the option, else `prj.conf`. The values shown are what the next
  build will use, and the status bar says how many options a rebuild changes.
- **Problems** (`p`): lines in your conf files that have no effect (a dependency is off,
  something else sets the option again later) or name an option that doesn't exist.

## Keys

| Key | Action |
| --- | --- |
| `space` | switch the option on / off |
| `enter` | change the option (on menus: fold / unfold) |
| `d` | remove it from your conf file |
| `/` | search; `esc` closes the search |
| `A` | show hidden options too |
| `tab` | go to the related options; `enter` jumps, `backspace` comes back |
| `p` | problems in your conf files |
| `o` | open where the option is set (or defined) in `$EDITOR` |
| `t` / `u` / `r` | pick another build / undo / reload |
| `?` | help |
