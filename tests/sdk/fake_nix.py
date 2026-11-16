"""Stands in for nix: `eval` lists zephyr-nix's SDKs, `build` links a fake SDK.

Builds go into $FAKE_NIX_STORE, one folder per build, like the Nix store.
Appends its arguments as a JSON line to $FAKE_LOG.
"""

import json
import os
import re
import sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["nix", *args]) + "\n")
VERSIONS = {"sdk-0_16": "0.16.9", "sdk-0_17": "0.17.4", "sdk-1_0": "1.0.1"}

if "eval" in args:
    print(json.dumps(VERSIONS))
    sys.exit(0)

assert "build" in args, args
link = Path(args[args.index("--out-link") + 1])
expr = args[args.index("--expr") + 1]
attr = re.search(r'"((?:sdk|hosttools)-[0-9_]+)"', expr).group(1)
store = Path(os.environ["FAKE_NIX_STORE"])
out = store / f"{len(list(store.glob('*'))):04d}-{attr}"
out.mkdir(parents=True)
if attr.startswith("hosttools-"):
    (out / "bin").mkdir()
else:
    version = VERSIONS[attr]
    (out / "sdk_version").write_text(version + "\n")
    targets = re.findall(r'"([^"]+)"', expr.split("targets = [", 1)[1].split("]", 1)[0])
    # Like zephyr-nix: SDK 1.0 and later keep GNU toolchains under gnu/.
    base = out / "gnu" if version.startswith("1.") else out
    for tc in targets:
        gcc = base / tc / "bin" / f"{tc}-gcc"
        gcc.parent.mkdir(parents=True)
        gcc.write_text("")
print(f"building {attr}", flush=True)
if link.is_symlink():
    link.unlink()
link.symlink_to(out)
print(out)
