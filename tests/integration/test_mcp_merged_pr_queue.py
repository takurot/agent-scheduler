from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from subsched.github.issues import GitHubIssueSource
from subsched.github.pull_requests import MergedPrCheckKind, MergedPrCheckResult
from subsched.mcp_server import queue_issues
from subsched.models import Capacity, CapacityState, Issue, Task, TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore


@pytest.mark.parametrize("dry_run", [True, False])
@pytest.mark.parametrize("kind", [MergedPrCheckKind.CONFIRMED, MergedPrCheckKind.AMBIGUOUS])
def test_mcp_merge_guard_preview_and_queue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, kind: MergedPrCheckKind
) -> None:
    (tmp_path / "subsched.yaml").write_text("github:\n  repo: acme/widgets\n")
    issue = Issue(number=129, title="Already implemented", labels=("ai-ready",))
    monkeypatch.setattr(GitHubIssueSource, "list_open", lambda *a, **kw: (issue,))
    calls: list[tuple[str, int]] = []

    def check(repo: str, number: int) -> MergedPrCheckResult:
        calls.append((repo, number))
        return MergedPrCheckResult(kind, pr_number=131, reason="unconfirmed merged PR")

    monkeypatch.setattr("subsched.mcp_server.check_merged_pr_for_issue", check, raising=False)
    result = queue_issues(str(tmp_path), dry_run=dry_run)
    assert calls == [("acme/widgets", 129)]
    assert result["would_queue" if dry_run else "queued"] == (
        0 if kind is MergedPrCheckKind.CONFIRMED else 1
    )
    if kind is MergedPrCheckKind.CONFIRMED:
        assert result["issue_numbers"] == []
        assert result["excluded"][0]["issue_number"] == 129
        assert "131" in result["excluded"][0]["reason"]
    else:
        assert result["needs_human"] == [{"issue_number": 129, "reason": "unconfirmed merged PR"}]
    if dry_run:
        assert not (tmp_path / ".ai").exists()
        return

    store = JsonStateStore(tmp_path)
    tasks = store.load_tasks()
    if kind is MergedPrCheckKind.CONFIRMED:
        assert tasks == ()
    else:
        assert tasks[0].status is TaskState.NEEDS_HUMAN
        assert tasks[0].needs_human_reason == "unconfirmed merged PR"
    worker = ScriptedWorker({})
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=store.worktrees_dir,
        merged_pr_checker=lambda number: check("acme/widgets", number),
    )
    scheduler.discover((issue,))
    now = datetime.now(UTC)
    assert not scheduler.tick(
        (
            Capacity(
                agent="claude",
                state=CapacityState.AVAILABLE,
                observed_at=now,
                source="provider",
                confidence="high",
            ),
        ),
        now=now,
    )
    assert worker.dispatches == []


@pytest.mark.parametrize("kind", [MergedPrCheckKind.CONFIRMED, MergedPrCheckKind.AMBIGUOUS])
@pytest.mark.parametrize("selected", [True, False])
@pytest.mark.parametrize(
    "status",
    [
        TaskState.READY,
        TaskState.WAITING_CAPACITY,
        TaskState.WAITING_DEPENDENCY,
        TaskState.BLOCKED,
        TaskState.RETRY,
        TaskState.NEEDS_REBASE,
    ],
)
def test_legacy_queued_tasks_are_held_before_later_dispatch(
    tmp_path: Path, kind: MergedPrCheckKind, selected: bool, status: TaskState
) -> None:
    store = JsonStateStore(tmp_path)
    issue = Issue(number=129, title="Legacy unchecked task")
    worktree = store.worktrees_dir / "issue-129"
    worktree.mkdir(parents=True)
    marker = worktree / "prior-work.txt"
    marker.write_text("preserve this work")
    task = replace(Task.from_issue(issue), status=status, worktree=str(worktree))
    store.save_tasks((task,))
    calls: list[int] = []

    def check(number: int) -> MergedPrCheckResult:
        calls.append(number)
        return MergedPrCheckResult(kind, pr_number=131, reason="unconfirmed merged PR")

    worker = ScriptedWorker({})
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=store.worktrees_dir,
        merged_pr_checker=check,
    )
    scheduler.discover((issue,) if selected else ())
    assert calls == [129]
    assert scheduler.tasks[0].status is TaskState.NEEDS_HUMAN
    assert scheduler.tasks[0].completion_kind is None
    assert scheduler.tasks[0].worktree == str(worktree)
    assert scheduler.discovery_notes
    now = datetime.now(UTC)
    assert not scheduler.tick(
        (
            Capacity(
                agent="claude",
                state=CapacityState.AVAILABLE,
                observed_at=now,
                source="provider",
                confidence="high",
            ),
        ),
        now=now,
    )
    assert worker.dispatches == []
    assert store.load_tasks()[0].status is TaskState.NEEDS_HUMAN
    assert marker.read_text() == "preserve this work"


@pytest.mark.parametrize("kind", [MergedPrCheckKind.CONFIRMED, MergedPrCheckKind.AMBIGUOUS])
def test_mcp_queue_then_later_merge_check_prevents_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: MergedPrCheckKind
) -> None:
    (tmp_path / "subsched.yaml").write_text("github:\n  repo: acme/widgets\n")
    issue = Issue(number=129, title="Initially no merged PR", labels=("ai-ready",))
    monkeypatch.setattr(GitHubIssueSource, "list_open", lambda *a, **kw: (issue,))
    monkeypatch.setattr(
        "subsched.mcp_server.check_merged_pr_for_issue",
        lambda repo, number: MergedPrCheckResult(MergedPrCheckKind.NONE),
    )
    assert queue_issues(str(tmp_path))["queued"] == 1
    store = JsonStateStore(tmp_path)
    assert store.load_tasks()[0].status is TaskState.READY
    snapshot = store.load_snapshot()
    monkeypatch.setattr(
        "subsched.mcp_server.check_merged_pr_for_issue",
        lambda repo, number: MergedPrCheckResult(kind, pr_number=131, reason="uncertain merge"),
    )
    assert queue_issues(str(tmp_path), dry_run=True)["would_queue"] == 0
    assert store.load_snapshot() == snapshot
    worker = ScriptedWorker({})
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        worktree_root=store.worktrees_dir,
        merged_pr_checker=lambda number: MergedPrCheckResult(
            kind, pr_number=131, reason="uncertain merge"
        ),
    )
    scheduler.discover((issue,))
    assert scheduler.tasks[0].status is TaskState.NEEDS_HUMAN
    now = datetime.now(UTC)
    assert not scheduler.tick(
        (
            Capacity(
                agent="claude",
                state=CapacityState.AVAILABLE,
                observed_at=now,
                source="provider",
                confidence="high",
            ),
        ),
        now=now,
    )
    assert worker.dispatches == []
