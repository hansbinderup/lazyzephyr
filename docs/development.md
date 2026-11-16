# Development

## Tests

The Tests tab's tests use `tests/twister/fake_twister.py` in place of twister. The
Devicetree, Kconfig and Build tests need a Zephyr tree: set `LAZYZEPHYR_TEST_ZEPHYR_BASE`
(or `ZEPHYR_BASE`) to one, else they are skipped. A shallow clone is enough:

```sh
git clone --depth 1 --branch v4.5.0 https://github.com/zephyrproject-rtos/zephyr /tmp/zephyr
LAZYZEPHYR_TEST_ZEPHYR_BASE=/tmp/zephyr pytest
```

## zephyr-nix in the SDK tab

The SDK tab's Nix backend uses a fixed zephyr-nix commit, `ZEPHYR_NIX` in
`lazyzephyr/sdk/backends.py`. Bump it when zephyr-nix adds an SDK version that a
supported Zephyr needs (`sdks/` in zephyr-nix lists them):

```sh
git ls-remote https://github.com/nix-community/zephyr-nix HEAD
```

Check that the attribute names (`sdk-1_0`, `hosttools-1_0`) and `sdk.override { targets }`
still work: `tests/sdk/fake_nix.py` copies them.

## Zephyr and Python versions in CI

lazyzephyr runs Zephyr's own Python code (edtlib, kconfiglib) in its interpreter, so CI
only tests the Python versions each Zephyr release supports. Zephyr already tests its
scripts on those Pythons, so CI tests two axes instead of every combination:

- **Python axis**: each Python lazyzephyr supports, against the newest Zephyr release
  that supports that Python.
- **Zephyr axis**: Python 3.12 against the other recent releases and `main`. Every
  release from 4.2 on supports 3.12, and Zephyr recommends it.

The releases are pinned tags (the newest patch of each minor), so CI only changes when
we change it. `main` runs on PRs but doesn't fail them. The nightly workflow
(`nightly.yml`, 03:00 CET) also tests `main` on its oldest and newest supported Python,
so an upstream break shows up the next morning. GitHub emails whoever last changed
`nightly.yml` when it fails, and turns scheduled workflows off after 60 days without
activity in the repository; re-enable it in the Actions tab.

Which Pythons a Zephyr release supports is the list in its own CI:

```sh
git clone --depth 1 --branch v4.5.0 https://github.com/zephyrproject-rtos/zephyr /tmp/z
grep -rh "python-version: \[" /tmp/z/.github/workflows/ | sort -u
```

| Zephyr | Python (Zephyr's CI) |
| --- | --- |
| 4.2, 4.3 | 3.10–3.13 |
| 4.4 | 3.12–3.13 |
| 4.5, `main` (Nov 2026) | 3.12–3.14 |

**When Zephyr makes a release** (`git ls-remote --tags https://github.com/zephyrproject-rtos/zephyr 'v4.*'`):

1. A new patch release (v4.5.1): replace the old tag of that minor in
   `.github/workflows/ci.yml`.
2. A new minor (v4.6.0): look up its Pythons as above and add a row to the table.
   Move the Python axis entries it supports to it, add the previous newest release to
   the Zephyr axis, and drop the oldest release from it. Keep about four releases.
3. A Python missing from the newest release stays on the newest release that still
   supports it (like 3.11 on v4.3.2). Once no tested release supports it, drop it
   from `requires-python` and the classifiers in `pyproject.toml`.
4. A new Python lazyzephyr supports: add it to the classifiers and to the Python axis.
5. If Zephyr's `main` drops or adds a Python, update the nightly matrix too.
