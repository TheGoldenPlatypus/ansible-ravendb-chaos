#!/usr/bin/env python3
"""Assert W-7 reader's history-fetch latency didn't drift more than `max_ratio`
between the first-quarter and last-quarter sample windows.

Per the RV-1 plan, phase 3's invariant is "no monotonic latency growth >
2x baseline".  W-7 reader logs one `elapsed_ms=N` per sample (default every
30s) for the full revision-history GET.  We take the mean over the first
quarter of samples (the "baseline" window, early in the run) and the mean
over the last quarter, and assert `last/first <= max_ratio`.

We use mean -- not p99 -- because at the default 30s sample rate over a
60-minute run we only get ~120 samples; p99 over a quarter of that is
1-2 points, too noisy for a stable threshold.

Usage:  assert_latency_drift.py <log_path> <max_ratio>
Exit:   0 PASS, 1 FAIL, 2 usage error.
"""

import re
import sys


def main():
    if len(sys.argv) != 3:
        print("usage: assert_latency_drift.py <log_path> <max_ratio>", file=sys.stderr)
        sys.exit(2)
    log_path, max_ratio = sys.argv[1], float(sys.argv[2])

    samples = []
    try:
        with open(log_path) as f:
            for line in f:
                if "history_fetch" not in line:
                    continue
                m = re.search(r"elapsed_ms=(\d+)", line)
                if m:
                    samples.append(int(m.group(1)))
    except FileNotFoundError:
        print("FAIL  W-7 reader log %s does not exist" % log_path)
        sys.exit(1)

    n = len(samples)
    if n < 8:
        print("FAIL  too few latency samples (%d) to compute first/last-quarter means" % n)
        sys.exit(1)

    q = n // 4
    first_mean = sum(samples[:q]) / q
    last_mean = sum(samples[-q:]) / q
    ratio = last_mean / first_mean if first_mean else float("inf")

    print("latency drift check  log=%s" % log_path)
    print("  total samples: %d  (quarter window = %d samples)" % (n, q))
    print("  first-quarter mean elapsed_ms: %.1f" % first_mean)
    print("  last-quarter  mean elapsed_ms: %.1f" % last_mean)
    print("  ratio (last / first):          %.3f (must be <= %.1f)" % (ratio, max_ratio))

    if ratio > max_ratio:
        print("FAIL  phase-3 read latency drifted > %.1fx baseline -- regression" % max_ratio)
        sys.exit(1)
    print("PASS  read latency within %.1fx baseline across the phase-3 window" % max_ratio)


if __name__ == "__main__":
    main()
