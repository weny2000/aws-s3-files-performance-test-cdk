#!/usr/bin/env python3
"""Format benchmark results JSON (read from stdin) as a summary table.

Usage:
    aws ssm get-parameter --name /benchmark/results \\
        --query Parameter.Value --output text | python3 scripts/print_results.py
"""
import sys
import json


def _size_sort_key(key: str) -> int:
    """Sort '100mb' < '1024mb' numerically; fall back to 0 for non-numeric keys."""
    try:
        return int(key.replace("mb", ""))
    except ValueError:
        return 0


def print_table(size_label: str, results: dict) -> None:
    SEP = "=" * 74
    hdr = (
        f"{'Case':<42}"
        f" {'AvgRead':>9}"
        f" {'AvgWrite':>9}"
        f" {'P95R':>8}"
        f" {'P95W':>8}"
        f" {'DL avg':>8}"
    )
    print()
    print(SEP)
    print(f"  BENCHMARK RESULTS — {size_label.upper()}")
    print(SEP)
    print(hdr)
    print("-" * len(hdr))
    for k, v in results.items():
        if "error" in v:
            print(f"{k:<42}  ERROR")
            continue
        label = v.get("label", k)
        print(
            f"{label:<42}"
            f"  {v.get('avg_read_mbps',   0):>7.1f}MB/s"
            f"  {v.get('avg_write_mbps',  0):>7.1f}MB/s"
            f"  {v.get('p95_read_ms',     0):>6.0f}ms"
            f"  {v.get('p95_write_ms',    0):>6.0f}ms"
            f"  {v.get('avg_download_ms', 0):>6.0f}ms"
        )
    print()


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        print("No data received on stdin.", file=sys.stderr)
        sys.exit(1)

    data = json.loads(raw)

    # Size-grouped format: {"100mb": {"case1": {...}}, "1024mb": {...}}
    # Legacy flat format:  {"case1": {...}, ...}
    if any(k.endswith("mb") for k in data):
        for size_key in sorted(data.keys(), key=_size_sort_key):
            print_table(size_key, data[size_key])
    else:
        print_table("results", data)


if __name__ == "__main__":
    main()
