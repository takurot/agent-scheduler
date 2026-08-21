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
