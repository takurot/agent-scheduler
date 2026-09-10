"""Branch coverage gate checker.

Calculates branch coverage percentage from a coverage JSON report:
  branch_coverage = (covered_branches / num_branches) * 100
If num_branches is 0, branch coverage is treated as 100.0%.
Fails (exits with code 1) if branch coverage is below the specified threshold (default: 80.0%).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def calculate_branch_coverage(coverage_data: dict[str, Any]) -> tuple[int, int, float]:
    totals = coverage_data.get("totals", {})
    num_branches = int(totals.get("num_branches", 0))
    covered_branches = int(totals.get("covered_branches", 0))
    percent = 100.0 if num_branches == 0 else (covered_branches / num_branches) * 100.0
    return covered_branches, num_branches, percent


def check_branch_coverage(json_path: Path, threshold: float = 80.0) -> int:
    if not json_path.exists():
        print(f"Error: coverage file '{json_path}' not found", file=sys.stderr)
        return 1
    with json_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    covered, total, percent = calculate_branch_coverage(data)
    print(f"Branch coverage: {covered}/{total} ({percent:.2f}%) [threshold: {threshold:.1f}%]")
    if percent < threshold:
        print(
            f"Error: branch coverage {percent:.2f}% is below required {threshold:.1f}% gate",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check branch coverage gate")
    parser.add_argument(
        "json_file",
        nargs="?",
        default="coverage.json",
        type=Path,
        help="Path to coverage.json (default: coverage.json)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=80.0,
        help="Minimum branch coverage percentage required (default: 80.0)",
    )
    args = parser.parse_args(argv)
    return check_branch_coverage(args.json_file, args.threshold)


if __name__ == "__main__":
    sys.exit(main())
