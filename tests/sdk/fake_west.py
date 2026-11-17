"""Stands in for `west sdk install` (lays out an SDK the way the real one does) and
`west packages pip --install`.

Appends its arguments as a JSON line to $FAKE_LOG.
"""

import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["west", *args]) + "\n")
if args[:3] == ["packages", "pip", "--install"]:
    assert os.environ.get("FAKE_VENV"), "not run from the venv"
    print("Successfully installed pyelftools-0.32 pykwalify-1.8.0 west-1.5.0 …", flush=True)
    sys.exit(0)
assert args[:2] == ["sdk", "install"], args
# Older Zephyr's FindZephyr-sdk.cmake breaks on newer CMake without it.
variant = os.environ.get("ZEPHYR_TOOLCHAIN_VARIANT")
assert variant == os.environ.get("FAKE_VARIANT", "zephyr"), variant


def value(flag):
    return args[args.index(flag) + 1]


version = value("--version")
print(f"Found 'zephyr/SDK_VERSION', installing version {version}.", flush=True)
sdk = Path(value("--install-base")) / f"zephyr-sdk-{version}"
sdk.mkdir(parents=True, exist_ok=True)
(sdk / "sdk_version").write_text(version + "\n")
toolchains = []
if "--gnu-toolchains" in args:
    rest = args[args.index("--gnu-toolchains") + 1 :]
    toolchains = [a for a in rest[: next((i for i, a in enumerate(rest) if a[:2] == "--"), None)]]
for tc in toolchains:
    gcc = sdk / "gnu" / tc / "bin" / f"{tc}-gcc"
    gcc.parent.mkdir(parents=True, exist_ok=True)
    gcc.write_text("")
    print(f"Installing '{tc}' GNU toolchain ...", flush=True)
if "--no-hosttools" not in args:
    (sdk / "hosttools").mkdir(exist_ok=True)
sys.exit(1 if "FAKE_FAIL" in os.environ else 0)
