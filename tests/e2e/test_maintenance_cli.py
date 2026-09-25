from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from subsched.cli import app
from subsched.maintenance import ArchiveDecision, ArchiveResult, MaintenanceReport
from subsched.models import Task, TaskState
from subsched.storage import JsonStateStore


def test_maintenance_json_dry_run_reports_usage_without_mutation(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    worktree = store.worktrees_dir / "issue-1"
    worktree.mkdir()
    task = Task(
        task_id="github-1",
        issue_number=1,
        title="Task 1",
        labels=(),
        status=TaskState.IN_PROGRESS,
        worktree=str(worktree),
    )
    store.save_tasks((task,))
    before = store.path.read_bytes()

    result = CliRunner().invoke(
        app,
        ["--repository", str(tmp_path), "maintenance", "--dry-run", "--json"],
        env={"NO_COLOR": "1"},
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["usage"]["worktrees"]["bytes"] >= 0
    assert payload["worktrees"][0]["eligible"] is False
    assert store.path.read_bytes() == before
    assert not (store.state_dir / "archive").exists()


def test_maintenance_rejects_conflicting_dry_run_and_apply(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        ["--repository", str(tmp_path), "maintenance", "--dry-run", "--apply"],
        env={"NO_COLOR": "1"},
    )

    assert result.exit_code != 0
    assert "cannot be combined" in result.output


def test_maintenance_apply_failure_is_nonzero_and_machine_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = MaintenanceReport(
        revision=1,
        usage={},
        worktrees=(ArchiveDecision(1, ".ai/worktrees/issue-1", True, ()),),
    )
    monkeypatch.setattr("subsched.cli.build_maintenance_report", lambda *_args: report)
    monkeypatch.setattr(
        "subsched.cli.apply_archive_plan",
        lambda *_args: (ArchiveResult(1, False, "revalidation failed"),),
    )

    result = CliRunner().invoke(
        app,
        ["--repository", str(tmp_path), "maintenance", "--apply", "--json"],
        env={"NO_COLOR": "1"},
    )

    assert result.exit_code == 1
    assert json.loads(result.output)["results"][0]["reason"] == "revalidation failed"
