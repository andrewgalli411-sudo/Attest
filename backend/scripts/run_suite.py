#!/usr/bin/env python
"""Run the whole test suite and say whether the RESULT can be trusted.

A laptop that goes to sleep mid-run (lid closed on battery, which `caffeinate` cannot prevent) stalls browsers and dev
servers, and tests then fail with timeouts that look like bugs. This wrapper watches the wall clock against the
monotonic clock (which stops while the machine sleeps) and, if the machine slept, reports the run as INVALID rather than
as failures.

    uv run python scripts/run_suite.py [pytest args...]

Exit codes: 0 all passed, 1 real failures, 3 the machine slept during the run (rerun it; do not investigate the failures).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time

SLEEP_GAP_S = 20.0  # wall time that passed without the monotonic clock noticing


def main() -> int:
    slept: list[float] = []
    stop = threading.Event()

    def watch() -> None:
        w0, m0 = time.time(), time.monotonic()
        while not stop.wait(1.0):
            w1, m1 = time.time(), time.monotonic()
            gap = (w1 - w0) - (m1 - m0)
            if gap > SLEEP_GAP_S:
                slept.append(gap)
                w0, m0 = w1, m1  # count each sleep once

    threading.Thread(target=watch, daemon=True).start()
    cmd = ["caffeinate", "-dimsu", sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *sys.argv[1:]] if sys.platform == "darwin" else [
        sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *sys.argv[1:]]
    started = time.monotonic()
    rc = subprocess.call(cmd)
    stop.set()
    mins = (time.monotonic() - started) / 60
    if slept:
        print(f"\nINVALID RUN: the machine slept for {sum(slept) / 60:.1f} minutes during this run ({len(slept)} time(s)). "
              "Failures above are not trustworthy. Keep the lid open (or plug in) and run again.")
        return 3
    print(f"\nrun finished in {mins:.1f} minutes with the machine awake the whole time (pytest exit code {rc})")
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
