# lazydts

A lazygit-style terminal UI to browse and edit a Zephyr target's devicetree.

- Pick a target: an existing build directory (exact, uses its `build_info.yml`) or any
  board, with your app's overlays and shields.
- Browse the final tree, with labels, status, compatible, binding, and whether a driver
  in the workspace uses it. See every file and line that defines or overrides a node.
- Edit only what the bindings allow: types, enums and labels are checked, and every edit
  is parsed again with Zephyr's own `edtlib`. If the tree no longer parses, the file is
  put back.
- `a` opens a searchable list of every binding, limited to ones that fit the selected
  node (e.g. I2C devices under an I2C bus), and asks for `reg` and required properties.
- Edits go to the right place: into an existing block, or a new `&label { };` in the
  file you pick (`w`). Zephyr's and third-party module files are never edited.

Zephyr only, not Linux devicetrees.

## Run

    nix run github:hansbinderup/lazydts      # once pushed
    nix run .                                # from a clone

Inside a west workspace:

    lazydts                                  # last target here, or a picker
    lazydts -b native_sim/native/64          # board + overlays of the app in cwd
    lazydts -d build                         # an existing build
    west lazydts ...                         # as a west extension

Press `?` for keys.

## Tests

    LAZYDTS_TEST_ZEPHYR_BASE=/path/to/zephyr pytest

Tests that need Zephyr's devicetree library are skipped without it.
