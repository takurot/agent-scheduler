"""Read-only explanation of the same queue/router decision used for dispatch."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from subsched.cli import app
from subsched.config import ExecutionConfig, SchedulerConfig
from subsched.explain import explain_issue
from subsched.mcp_server import explain_task
from subsched.models import Capacity, CapacityState, Issue, Task, TaskState
from subsched.storage import JsonStateStore

NOW = datetime(2026, 9, 21, tzinfo=UTC)
runner = CliRunner()


def _capacity(*, observed_at: datetime = NOW) -> Capacity:
    return Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=observed_at,
        source="provider",
        confidence="high",
    )


def test_ready_issue_matches_router_and_queue_choice() -> None:
    issue = Issue(number=2, title="Selected")
    tasks = (Task.from_issue(issue),)

    result = explain_issue(issue, tasks, SchedulerConfig(), (_capacity(),), now=NOW)

    assert result["dispatchable"] is True
    assert result["selected_provider"] == "claude"
    assert result["reason_codes"] == []


def test_excluded_label_dependency_cycle_budget_and_stale_capacity_have_distinct_reasons() -> None:
    cfg = SchedulerConfig(execution=ExecutionConfig(max_tasks_per_run=1))
    excluded = Issue(number=2, title="Excluded", labels=("security-sensitive",))
    assert "EXCLUDED_LABEL" in explain_issue(excluded, (), cfg, (), now=NOW)["reason_codes"]

    parent = Task.from_issue(Issue(number=1, title="Parent"))
    child_issue = Issue(number=2, title="Child", body="Blocked-By: #1")
    child = replace(Task.from_issue(child_issue), status=TaskState.WAITING_DEPENDENCY)
    assert "UNFINISHED_DEPENDENCY" in explain_issue(
        child_issue, (parent, child), cfg, (_capacity(),), now=NOW
    )["reason_codes"]

    cycle_issue = Issue(number=3, title="Cycle", body="Blocked-By: #3")
    cycle = replace(Task.from_issue(cycle_issue), status=TaskState.BLOCKED)
    assert "DEPENDENCY_CYCLE" in explain_issue(
        cycle_issue, (cycle,), cfg, (_capacity(),), now=NOW
    )["reason_codes"]

    ready_issue = Issue(number=4, title="Ready")
    ready = Task.from_issue(ready_issue)
    assert "RUN_BUDGET_EXCEEDED" in explain_issue(
        ready_issue, (ready,), cfg, (_capacity(),), now=NOW, run_dispatched_issues={1}
    )["reason_codes"]
    assert "CAPACITY_STALE" in explain_issue(
        ready_issue, (ready,), cfg, (_capacity(observed_at=NOW - timedelta(hours=1)),), now=NOW
    )["reason_codes"]


def test_explain_is_strictly_read_only_and_preserves_state(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    issue = Issue(number=10, title="Preserved")
    task = Task.from_issue(issue)
    store.save_state((task,), capacities=(_capacity(),))

    original_tasks = store.load_tasks()
    original_caps = store.load_capacities()
    original_rev = store.get_revision()

    result = explain_issue(issue, original_tasks, SchedulerConfig(), original_caps, now=NOW)
    assert result["dispatchable"] is True
    assert store.load_tasks() == original_tasks
    assert store.load_capacities() == original_caps
    assert store.get_revision() == original_rev


def test_explain_cli_and_mcp_integration(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    store = JsonStateStore(tmp_path)
    issue = Issue(number=42, title="Diagnosed Issue")
    task = replace(
        Task.from_issue(issue),
        status=TaskState.NEEDS_HUMAN,
        needs_human_reason="Broken handoff",
    )
    store.save_state((task,), capacities=(_capacity(),))

    # Test MCP explain_task
    mcp_result = explain_task(42, str(tmp_path))
    assert mcp_result["issue_number"] == 42
    assert mcp_result["dispatchable"] is False
    assert "NEEDS_HUMAN" in mcp_result["reason_codes"]
    assert "Broken handoff" in mcp_result["reasons"][0]

    # Test CLI --json
    result_json = runner.invoke(app, ["--repository", str(tmp_path), "explain", "42", "--json"])
    assert result_json.exit_code == 0
    parsed = json.loads(result_json.output)
    assert parsed["issue_number"] == 42
    assert parsed["dispatchable"] is False
    assert "NEEDS_HUMAN" in parsed["reason_codes"]

    # Test CLI human readable
    result_text = runner.invoke(app, ["--repository", str(tmp_path), "explain", "42"])
    assert result_text.exit_code == 0
    assert "Issue #42: Diagnosed Issue" in result_text.output
    assert "Dispatchable:       NO" in result_text.output
    assert "Reason Codes:       NEEDS_HUMAN" in result_text.output
