from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.check_branch_coverage import (
    calculate_branch_coverage,
    check_branch_coverage,
    main,
)


def test_calculate_branch_coverage_normal() -> None:
    data = {
        "totals": {
            "covered_branches": 84,
            "num_branches": 100,
        }
    }
    covered, total, percent = calculate_branch_coverage(data)
    assert covered == 84
    assert total == 100
    assert percent == 84.0


def test_calculate_branch_coverage_zero_branches() -> None:
    data = {
        "totals": {
            "covered_branches": 0,
            "num_branches": 0,
        }
    }
    covered, total, percent = calculate_branch_coverage(data)
    assert covered == 0
    assert total == 0
    assert percent == 100.0


def test_check_branch_coverage_passes_above_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps({"totals": {"covered_branches": 85, "num_branches": 100}}),
        encoding="utf-8",
    )

    exit_code = check_branch_coverage(report, threshold=80.0)
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "Branch coverage: 85/100 (85.00%) [threshold: 80.0%]" in captured.out


def test_check_branch_coverage_fails_below_threshold(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps({"totals": {"covered_branches": 79, "num_branches": 100}}),
        encoding="utf-8",
    )

    exit_code = check_branch_coverage(report, threshold=80.0)
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "below required 80.0% gate" in captured.err


def test_check_branch_coverage_fails_on_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nonexistent.json"
    exit_code = check_branch_coverage(missing, threshold=80.0)
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "not found" in captured.err


def test_main_cli(tmp_path: Path) -> None:
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps({"totals": {"covered_branches": 90, "num_branches": 100}}),
        encoding="utf-8",
    )

    assert main([str(report), "--threshold", "80.0"]) == 0
    assert main([str(report), "--threshold", "95.0"]) == 1
