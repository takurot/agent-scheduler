from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from subsched.cli import app
from subsched.models import Issue, Task, TaskState
from subsched.storage import JsonStateStore

runner = CliRunner()


def test_status_verbose_output(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    task1 = Task.from_issue(Issue(number=101, title="Task one"))
    task1 = (
        task1.transition(TaskState.DISPATCHED, current_agent="claude")
        .transition(TaskState.IN_PROGRESS, current_agent="claude")
        .transition(TaskState.VERIFYING, current_agent="claude")
        .transition(TaskState.PR_READY, current_agent="claude")
        .transition(TaskState.READY_FOR_REVIEW, current_agent="claude")
        .transition(TaskState.COMPLETE, current_agent="claude")
    )
    task2 = Task.from_issue(Issue(number=102, title="Task two"))
    task2 = task2.transition(TaskState.DISPATCHED, current_agent="codex")
    store.save_tasks((task1, task2))

    res = runner.invoke(app, ["--repository", str(tmp_path), "status", "--verbose"])
    assert res.exit_code == 0
    assert "#101" in res.output
    assert "#102" in res.output
    assert "codex" in res.output


def test_status_verbose_shows_needs_human_reason(tmp_path: Path) -> None:
    """Regression test for #128: a NEEDS_HUMAN task's escalation reason must be visible
    in `subsched status --verbose`, not silently discarded."""
    store = JsonStateStore(tmp_path)
    task = Task.from_issue(Issue(number=103, title="Task with a push failure"))
    task = (
        task.transition(TaskState.DISPATCHED, current_agent="claude")
        .transition(TaskState.IN_PROGRESS, current_agent="claude")
        .transition(TaskState.VERIFYING, current_agent="claude")
        .transition(
            TaskState.NEEDS_HUMAN,
            current_agent="claude",
            reason="push failed (PERMISSION_DENIED): denied",
        )
    )
    store.save_tasks((task,))

    res = runner.invoke(app, ["--repository", str(tmp_path), "status", "--verbose"])
    assert res.exit_code == 0
    assert "#103" in res.output
    assert "push failed (PERMISSION_DENIED): denied" in res.output


def test_status_verbose_shows_task_runtime_start(tmp_path: Path) -> None:
    """Regression test for #137: the durable Task-level runtime start (distinct from
    execution.agent_timeout_seconds, a per-invocation config value) must be visible in
    `subsched status --verbose`."""
    from dataclasses import replace
    from datetime import UTC, datetime

    store = JsonStateStore(tmp_path)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    task = replace(
        Task.from_issue(Issue(number=104, title="Long running task")),
        status=TaskState.READY,
        run_started_at=started,
    )
    store.save_tasks((task,))

    res = runner.invoke(app, ["--repository", str(tmp_path), "status", "--verbose"])
    assert res.exit_code == 0
    assert "#104" in res.output
    assert "task runtime since" in res.output
    assert started.isoformat() in res.output


def test_status_default_output_shows_upcoming_tasks_and_capacity(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from subsched.models import Capacity, CapacityState

    store = JsonStateStore(tmp_path)
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    reset = now + timedelta(hours=2)

    task1 = Task.from_issue(Issue(number=101, title="Task one"))
    task2 = Task.from_issue(Issue(number=102, title="Task two"))
    task3 = Task.from_issue(Issue(number=103, title="Task three"))
    task4 = Task.from_issue(Issue(number=104, title="Task four"))

    capacities = (
        Capacity(
            agent="claude",
            state=CapacityState.COOLDOWN_SESSION,
            scope="five_hour",
            used_percentage=100.0,
            reset_at=reset,
            observed_at=now,
            source="provider",
            confidence="high",
        ),
        Capacity(
            agent="codex",
            state=CapacityState.AVAILABLE,
            scope="seven_day",
            used_percentage=20.0,
            observed_at=now,
            source="provider",
            confidence="high",
        ),
    )
    store.save_state((task1, task2, task3, task4), capacities=capacities)


    res = runner.invoke(app, ["--repository", str(tmp_path), "status"])
    assert res.exit_code == 0
    # Task summary in default output (shows upcoming tasks and remaining count)
    assert "Upcoming Tasks:" in res.output
    assert "#101" in res.output
    assert "#102" in res.output
    assert "#103" in res.output
    assert "... and 1 more task(s)" in res.output
    # Capacity summary in default output
    assert "Capacity & Cooldown:" in res.output
    assert "claude (five_hour): COOLDOWN_SESSION [100.0% used]" in res.output
    assert reset.isoformat() in res.output
    assert "codex (seven_day): AVAILABLE [20.0% used]" in res.output

