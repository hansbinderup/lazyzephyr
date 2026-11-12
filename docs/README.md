# lazyzephyr docs

How to use each tab, and every key:

- [Build](build.md): build configs, flashing, the serial monitor
- [Devicetree](devicetree.md): browse and edit a target's devicetree
- [Kconfig](kconfig.md): a build's options, and changing them in your conf files
- [Tests](tests.md): running twister, the job queue, results, `.lazyzephyr.toml`

The footer always shows the keys of the tab you are in, and `?` opens that tab's help.

## Tabs

| Key | Action |
| --- | --- |
| `]` / `[` | next / previous tab |
| `F1` / `F2` / `F3` / `F4` | Build / Devicetree / Kconfig / Tests |
| `q` | quit (asks first if a twister job is running or queued) |

It starts in the Build tab; `--tab dts` (or giving `-b` / `-d`) starts in Devicetree,
`--tab tests` in Tests, `--tab kconfig` in Kconfig.

## Panels

Every tab's panels can be resized and moved, and each tab remembers it in
`~/.cache/lazyzephyr/layout.json`:

| Key | Action |
| --- | --- |
| drag a border | resize the two panels on either side of it |
| `+` / `-` | grow / shrink the focused panel |
| `<` / `>` | move the focused panel back / on (on the left column: swap the columns) |
| `=` | reset sizes and order |
| `v` | layout: auto, side by side, stacked |
| `z` | maximize the focused panel (`z` or `esc` again restores it) |
