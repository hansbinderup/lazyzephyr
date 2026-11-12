# Devicetree tab

## Features

- **Pick a target**: an existing build directory (exact: it uses the build's
  `build_info.yml`), or any board in the workspace, with your app's overlays and shields.
  Press `t` to switch. The last target is remembered per directory.
- **See the final tree**: labels, status, compatible, the binding, the bus, and whether a
  driver in the workspace uses the compatible. Details list every file and line that
  defines or overrides the node, across `.dts`, `.dtsi` and overlays.
- **Referenced by**: the details list every node that points at the selected one, and
  how (`&led0  gpios  pin=13 flags=1`), plus its aliases and chosen entries. Worth a look
  before you disable or delete a node.
- **Problems** (`p`): things that parse but won't work, marked `✖` / `⚠` in the tree:
  a pin used by two enabled nodes (GPIOs, and pinctrl on nRF, STM32 and Ambiq Apollo),
  two devices at one address on a bus, two enabled bus controllers sharing one register
  block (nRF `spi0` + `i2c0`), overlapping flash partitions, enabled nodes or chosen
  entries that point at a disabled node, and deprecated properties. `enter` jumps to it.
- **Settings**: press `enter` on a node to get its settings in plain words ("Clock
  frequency  400 kHz", "Enabled  ● yes"). What's set comes first, then the device's own
  settings, and the generic ones (interrupts, clocks, power management…) sit under
  "more settings". The node's devices are listed too, with `+ add device…`. The line
  under the list explains the highlighted setting and says which file a change goes to.
- **Changing a setting**: `space` switches on/off settings. `enter` opens a short list
  to pick from: the choices of an enum, the nodes for a reference, the matching macros
  for a number (`I2C_BITRATE_FAST  400 kHz`, from the `dt-bindings` headers the tree
  includes). Type to search, or type your own value; `↑↓` and `enter` set it. `d`
  removes a value so the board's applies again. Changes are made straight away, checked
  against the binding and parsed again with Zephyr's own `edtlib`; if the tree no longer
  parses, the change is put back and you see why.
- **A reference card per setting** (`i`): type, whether it's required, the full
  description, allowed values and default, every file and line that sets it, the format
  of `gpios`-style properties and which controllers fit, the macros that fit
  (`I2C_BITRATE_FAST  400 kHz`, `GPIO_ACTIVE_LOW`…), and a snippet for an overlay; `y`
  copies the snippet.
- **`gpios`, `pwms`, `io-channels` and other phandle-arrays** are edited as rows: pick the
  controller from the nodes that can serve it, then fill in its named fields (`pin`,
  `flags`, `channel`…). Pins are checked against the controller's `ngpios`. `ctrl+n` /
  `ctrl+r` add / remove an entry, `ctrl+t` switches to plain text and back.
- **`/chosen` and `/aliases`**: point an entry at another node, remove it, or
  `+ add` a new one. Names for `chosen` are suggested from what Zephyr's code reads
  (`DT_CHOSEN(zephyr_console)` and Kconfig's `DT_CHOSEN_*` symbols), e.g.
  `zephyr,shell-uart`.
- **Add a node** (`a`): a searchable list of every binding, limited to the ones that fit
  the selected node (e.g. I2C devices under an I2C bus). `✓` marks bindings a driver uses.
  It then asks for `reg` (the `@unit-address` comes from it), a label, the required
  properties and the file to write to.
- **Edits go to the right place**: into the node's existing block in that file, or a new
  `&label { };` block. `w` picks the file. Files in Zephyr itself and in third-party
  modules are never edited. If a later file overrides your change, you are told.
- Edits are written straight to your source files: `u` undoes the last one, and
  `git diff` shows them all. The tree reloads by itself when the files change.

## Keys

| Key | Action |
| --- | --- |
| `enter` | in the tree: go to the node's settings; in settings: change the setting |
| `space` | in settings: switch on/off settings |
| `i` | in settings: the setting's reference card (`y` copies its overlay snippet) |
| `esc` | back to the tree |
| `/` | filter by name, label or compatible |
| `a` | add a node under the selected node |
| `s` | turn the node on / off |
| `d` | delete the node, or in settings remove the value |
| `o` | open `$EDITOR` where the node or value is set |
| `p` | list problems; `enter` goes to the node |
| `w` / `t` | pick the file edits go to / pick another target |
| `u` / `r` | undo the last change / reload |
| `z` / `?` | maximise the focused panel / help |

A C preprocessor (`gcc` or `clang`) must be on `PATH`, as for any Zephyr build.
