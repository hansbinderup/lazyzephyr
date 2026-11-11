"""Stand-in for twister: parses the args lazyzephyr passes, prints twister-like
progress lines and writes a twister.json into the -O directory."""

import argparse
import json
import os
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("-T", action="append", default=[])
parser.add_argument("-s", action="append", default=[])
parser.add_argument("-p", action="append", default=[])
parser.add_argument("-G", action="store_true")
parser.add_argument("-i", action="store_true")
parser.add_argument("-v", action="count")
parser.add_argument("-j", type=int)
parser.add_argument("-O", required=True)
parser.add_argument("--clobber-output", action="store_true")
parser.add_argument("rest", nargs=argparse.REMAINDER)
args = parser.parse_args()

scenarios = args.s or [os.environ.get("FAKE_DEFAULT_SCENARIO", "drivers.beta")]
fail = set(filter(None, os.environ.get("FAKE_FAIL", "").split(",")))
delay = float(os.environ.get("FAKE_DELAY", "0"))
outdir = Path(args.O)
outdir.mkdir(parents=True, exist_ok=True)
(outdir / "argv.json").write_text(json.dumps(sys.argv[1:]))

suites = []
for index, name in enumerate(scenarios, 1):
    time.sleep(delay)
    status = "failed" if name in fail else "passed"
    print(f"INFO    - {index}/{len(scenarios)} native_sim  {name}  {status.upper()} (native 0.01s)",
          flush=True)
    suites.append({
        "name": name,
        "platform": "native_sim",
        "status": status,
        "execution_time": "0.01",
        "build_time": "1.5",
        "log": (Path(os.environ["FAKE_LOG"]).read_text() if os.environ.get("FAKE_LOG")
                else "assertion failed\n") if status == "failed" else None,
        "testcases": [{"identifier": "suite.case_a", "status": status}],
    })
(outdir / "twister.json").write_text(json.dumps({"environment": {}, "testsuites": suites}))
sys.exit(1 if any(s["status"] == "failed" for s in suites) else 0)
