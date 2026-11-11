"""Stands in for west: prints its arguments, fails if FAKE_WEST_FAIL is in them."""

import sys
import time

args = sys.argv[1:]
print("fake west", " ".join(args), flush=True)
if "-t" in args and args[args.index("-t") + 1] == "run":
    for i in range(100):
        print(f"tick {i}", flush=True)
        time.sleep(0.05)
for i in range(3):
    print(f"[{i + 1}/3] step", flush=True)
sys.exit(1 if "FAKE_WEST_FAIL" in " ".join(args) else 0)
