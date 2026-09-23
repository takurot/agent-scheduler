"""Tests for NEEDS_HUMAN resolution service, audit logging, and state recovery."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from subsched.cli import app
from subsched.mcp_server import resolve_needs_human
from subsched.models import Issue, Task, TaskState
from subsched.recovery import (
    ResolveError,
    RestoreError,
    resolve_needs_human_task,
    restore_state_from_snapshot,
)
from subsched.storage import SCHEMA_VERSION, JsonStateStore

runner = CliRunner()


def test_resolve_needs_human_preserves_sanitized_note_and_audit(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    store = JsonStateStore(tmp_path)
    dummy_ghp = "ghp_" + "secret1234567890abcdef"
    dummy_sk = "sk-" + "1234567890abcdef1234567890abcdef"
    issue = Issue(number=10, title="Needs human")
    task = replace(
        Task.from_issue(issue),
        status=TaskState.NEEDS_HUMAN,
        needs_human_reason=f"Broken environment: token={dummy_ghp}",
    )
    store.save_tasks((task,))

    secret_note = f"Fixed the bug using secret token {dummy_sk}"
    result = resolve_needs_human_task(
        store,
        10,
        note=secret_note,
        dry_run=False,
        now=datetime(2026, 9, 21, 12, 0, tzinfo=UTC),
    )

    assert result.issue_number == 10
    assert result.previous_status == TaskState.NEEDS_HUMAN
    assert result.new_status == TaskState.READY
    assert "sk-" not in result.resolution_note
    assert "[REDACTED]" in result.resolution_note

    # Task updated in store
    loaded = store.load_tasks()[0]
    assert loaded.status is TaskState.READY
    assert loaded.resolution_note is not None
    assert "[REDACTED]" in loaded.resolution_note
    assert "sk-" not in loaded.resolution_note

    # Audit log exists and is sanitized
    audit_file = tmp_path / ".ai" / "audit" / "resolutions.jsonl"
    assert audit_file.is_file()
    lines = audit_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    audit_entry = json.loads(lines[0])
    assert audit_entry["issue_number"] == 10
    assert "ghp_secret" not in audit_entry["previous_reason"]
    assert "sk-" not in audit_entry["resolution_note"]


def test_resolve_needs_human_dry_run_does_not_mutate(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    store = JsonStateStore(tmp_path)
    issue = Issue(number=10, title="Needs human")
    task = replace(
        Task.from_issue(issue),
        status=TaskState.NEEDS_HUMAN,
        needs_human_reason="Some blocker",
    )
    store.save_tasks((task,))
    original_rev = store.get_revision()

    result = resolve_needs_human_task(store, 10, note="Fix applied", dry_run=True)
    assert result.issue_number == 10
    assert result.new_status == TaskState.READY

    # State unchanged
    assert store.get_revision() == original_rev
    loaded = store.load_tasks()[0]
    assert loaded.status is TaskState.NEEDS_HUMAN


def test_resolve_rejects_non_needs_human_task(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    task = Task.from_issue(Issue(number=10, title="Ready"))
    store.save_tasks((task,))

    with pytest.raises(ResolveError, match="not in NEEDS_HUMAN"):
        resolve_needs_human_task(store, 10, note="Fix applied", dry_run=False)


def test_restore_state_rejects_unknown_schema_symlinks_and_conflicts(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.save_tasks((Task.from_issue(Issue(number=1, title="Initial")),))

    # 1. Unknown schema
    unknown_schema_file = tmp_path / "unknown_schema.json"
    unknown_schema_file.write_text(
        json.dumps({"schema_version": 999, "revision": 1, "tasks": []}),
        encoding="utf-8",
    )
    with pytest.raises(RestoreError, match="unknown schema"):
        restore_state_from_snapshot(store, unknown_schema_file)

    # 2. Symlink
    real_backup = tmp_path / "real_backup.json"
    real_backup.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "revision": 1, "tasks": []}),
        encoding="utf-8",
    )
    symlink_backup = tmp_path / "symlink_backup.json"
    symlink_backup.symlink_to(real_backup)
    with pytest.raises(RestoreError, match="symlink"):
        restore_state_from_snapshot(store, symlink_backup)


def test_restore_state_preserves_retry_and_runtime_limits(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.save_tasks(())

    backup_file = tmp_path / "valid_backup.json"
    task_dict = Task.from_issue(Issue(number=5, title="Attempted")).to_dict()
    task_dict["attempt"] = 3
    task_dict["run_started_at"] = "2026-09-20T10:00:00+00:00"

    backup_file.write_text(
        json.dumps({
            "schema_version": SCHEMA_VERSION,
            "revision": 5,
            "paused": False,
            "tasks": [task_dict],
            "capacities": [],
        }),
        encoding="utf-8",
    )

    result = restore_state_from_snapshot(store, backup_file, dry_run=False)
    assert result.restored_tasks_count == 1

    loaded = store.load_tasks()[0]
    assert loaded.issue_number == 5
    assert loaded.attempt == 3
    assert loaded.run_started_at == datetime(2026, 9, 20, 10, 0, 0, tzinfo=UTC)


def test_cli_resolve_and_restore_state(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    store = JsonStateStore(tmp_path)
    task = replace(
        Task.from_issue(Issue(number=15, title="Problem")),
        status=TaskState.NEEDS_HUMAN,
        needs_human_reason="Config error",
    )
    store.save_tasks((task,))

    # CLI resolve --dry-run
    res_dry = runner.invoke(
        app,
        ["--repository", str(tmp_path), "resolve", "15", "--note", "Fixed", "--dry-run"],
    )
    assert res_dry.exit_code == 0
    assert "DRY RUN" in res_dry.output
    assert store.load_tasks()[0].status is TaskState.NEEDS_HUMAN

    # CLI resolve
    res_apply = runner.invoke(
        app,
        ["--repository", str(tmp_path), "resolve", "15", "--note", "Fixed config"],
    )
    assert res_apply.exit_code == 0
    assert "Resolved" in res_apply.output
    assert store.load_tasks()[0].status is TaskState.READY

    # MCP resolve_needs_human uses the same service
    task2 = replace(
        Task.from_issue(Issue(number=16, title="Problem 2")),
        status=TaskState.NEEDS_HUMAN,
        needs_human_reason="Gate failed",
    )
    store.save_tasks((store.load_tasks()[0], task2))
    mcp_res = resolve_needs_human(
        16, resolution_notes="Gate resolved", repository_path=str(tmp_path)
    )
    assert mcp_res["issue_number"] == 16
    assert mcp_res["status"] == "READY"
    assert store.load_tasks()[1].status is TaskState.READY
