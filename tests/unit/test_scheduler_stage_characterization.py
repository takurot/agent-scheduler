"""Characterization tests for Scheduler multi-stage planning and PR review handling (#382).

Fixes behavior and side-effect order before and after modular stage handler extraction.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.config import WorkflowConfig, WorkflowLimitsConfig, WorkflowStagesConfig
from subsched.models import (
    AgentResult,
    AgentResultKind,
    Capacity,
    CapacityState,
    Issue,
    Task,
    TaskState,
)
from subsched.plan_review import PlanVerdict, plan_path
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _capacity(agent: str = "claude") -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        observed_at=NOW,
        source="provider",
        confidence="high",
        used_percentage=5,
        reset_at=NOW + timedelta(hours=5),
    )


def test_characterize_planning_approved_transition(tmp_path: Path) -> None:
    worktree = tmp_path / "worktrees" / "issue-1"
    worktree.mkdir(parents=True)
    plan_file = worktree / plan_path(1)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text("# Plan\nInitial proposal\n", encoding="utf-8")

    # ScriptedWorker simulates pass for planning and pass with APPROVE verdict for review
    worker = ScriptedWorker(
        {
            (1, "claude"): (
                AgentResult(AgentResultKind.PASS),
                AgentResult(
                    AgentResultKind.PASS,
                    plan_verdict=PlanVerdict(verdict="APPROVE", summary="LGTM", findings=()),
                ),
            )
        }
    )
    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        workflow=WorkflowConfig(mode="multi-stage"),
        worktree_root=tmp_path / "worktrees",
    )

    task = Task.from_issue(Issue(number=1, title="Test Issue"), worktree=str(worktree))
    task = task.transition(TaskState.DISPATCHED, current_agent="claude", now=NOW)
    scheduler.queue = scheduler.queue.append([task])

    # Run planning stage
    outcome, approved_task = scheduler._run_planning_and_review(
        task, "claude", NOW, lease_nonce="test-lease"
    )

    assert outcome == "approved"
    assert approved_task is not None
    assert approved_task.plan_approved is True
    assert approved_task.status is TaskState.IN_PROGRESS


def test_characterize_planning_opt_out_review_advances(tmp_path: Path) -> None:
    worktree = tmp_path / "worktrees" / "issue-10"
    worktree.mkdir(parents=True)
    plan_file = worktree / plan_path(10)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text("# Plan\nInitial proposal\n", encoding="utf-8")

    worker = ScriptedWorker({(10, "claude"): (AgentResult(AgentResultKind.PASS),)})
    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        workflow=WorkflowConfig(mode="multi-stage", stages=WorkflowStagesConfig(plan_review=False)),
        worktree_root=tmp_path / "worktrees",
    )

    task = Task.from_issue(Issue(number=10, title="Opt-out review"), worktree=str(worktree))
    task = task.transition(TaskState.DISPATCHED, current_agent="claude", now=NOW)
    scheduler.queue = scheduler.queue.append([task])

    outcome, approved_task = scheduler._run_planning_and_review(
        task, "claude", NOW, lease_nonce="test-lease"
    )

    assert outcome == "approved"
    assert approved_task is not None
    assert approved_task.plan_approved is True
    assert approved_task.status is TaskState.IN_PROGRESS


def test_characterize_planning_missing_plan_escalates_to_needs_human(tmp_path: Path) -> None:
    worktree = tmp_path / "worktrees" / "issue-2"
    worktree.mkdir(parents=True)
    # Plan file is NOT written

    worker = ScriptedWorker({(2, "claude"): (AgentResult(AgentResultKind.PASS),)})
    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        workflow=WorkflowConfig(mode="multi-stage"),
        worktree_root=tmp_path / "worktrees",
    )

    task = Task.from_issue(Issue(number=2, title="No Plan"), worktree=str(worktree))
    task = task.transition(TaskState.DISPATCHED, current_agent="claude", now=NOW)
    scheduler.queue = scheduler.queue.append([task])

    outcome, approved_task = scheduler._run_planning_and_review(
        task, "claude", NOW, lease_nonce="test-lease"
    )

    assert outcome == "terminal"
    assert approved_task is None
    escalated = scheduler.queue.get(2)
    assert escalated.status is TaskState.NEEDS_HUMAN
    assert "without producing a plan file" in (escalated.needs_human_reason or "")


def test_characterize_planning_worker_failure_routes_to_handle_result(tmp_path: Path) -> None:
    worktree = tmp_path / "worktrees" / "issue-3"
    worktree.mkdir(parents=True)

    worker = ScriptedWorker(
        {(3, "claude"): (AgentResult(AgentResultKind.FAILURE, output="planning error"),)}
    )
    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        workflow=WorkflowConfig(mode="multi-stage"),
        worktree_root=tmp_path / "worktrees",
    )

    task = Task.from_issue(Issue(number=3, title="Plan Failure"), worktree=str(worktree))
    task = task.transition(TaskState.DISPATCHED, current_agent="claude", now=NOW)
    scheduler.queue = scheduler.queue.append([task])

    outcome, approved_task = scheduler._run_planning_and_review(
        task, "claude", NOW, lease_nonce="test-lease"
    )

    assert outcome == "terminal"
    assert approved_task is None
    # Failed task routes through RETRY to READY (attempt 0 -> 1)
    retried = scheduler.queue.get(3)
    assert retried.status is TaskState.READY
    assert retried.attempt == 1
    assert retried.per_agent_failures == (("claude", 1),)


def test_characterize_plan_review_request_changes_then_approves(tmp_path: Path) -> None:
    worktree = tmp_path / "worktrees" / "issue-4"
    worktree.mkdir(parents=True)
    plan_file = worktree / plan_path(4)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text("# Plan v1\n", encoding="utf-8")

    worker = ScriptedWorker(
        {
            (4, "claude"): (
                AgentResult(AgentResultKind.PASS),  # round 1 planning
                AgentResult(  # round 1 review: request changes
                    AgentResultKind.PASS,
                    plan_verdict=PlanVerdict(
                        verdict="REQUEST_CHANGES", summary="Needs improvement", findings=()
                    ),
                ),
                AgentResult(AgentResultKind.PASS),  # round 2 planning
                AgentResult(  # round 2 review: approve
                    AgentResultKind.PASS,
                    plan_verdict=PlanVerdict(verdict="APPROVE", summary="LGTM", findings=()),
                ),
            )
        }
    )
    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        workflow=WorkflowConfig(mode="multi-stage"),
        worktree_root=tmp_path / "worktrees",
    )

    task = Task.from_issue(Issue(number=4, title="Revisions"), worktree=str(worktree))
    task = task.transition(TaskState.DISPATCHED, current_agent="claude", now=NOW)
    scheduler.queue = scheduler.queue.append([task])

    outcome, approved_task = scheduler._run_planning_and_review(
        task, "claude", NOW, lease_nonce="test-lease"
    )

    assert outcome == "approved"
    assert approved_task is not None
    assert approved_task.plan_approved is True
    assert approved_task.plan_revisions == 1
    assert approved_task.status is TaskState.IN_PROGRESS


def test_characterize_plan_review_exceeds_max_revisions_escalates(tmp_path: Path) -> None:
    worktree = tmp_path / "worktrees" / "issue-5"
    worktree.mkdir(parents=True)
    plan_file = worktree / plan_path(5)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text("# Plan\n", encoding="utf-8")

    worker = ScriptedWorker(
        {
            (5, "claude"): (
                AgentResult(AgentResultKind.PASS),
                AgentResult(
                    AgentResultKind.PASS,
                    plan_verdict=PlanVerdict(
                        verdict="REQUEST_CHANGES", summary="Unacceptable", findings=()
                    ),
                ),
            )
        }
    )
    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        workflow=WorkflowConfig(
            mode="multi-stage",
            limits=WorkflowLimitsConfig(max_plan_revisions=1),
        ),
        worktree_root=tmp_path / "worktrees",
    )

    task = Task.from_issue(Issue(number=5, title="Too Many Revisions"), worktree=str(worktree))
    task = task.transition(TaskState.DISPATCHED, current_agent="claude", now=NOW)
    scheduler.queue = scheduler.queue.append([task])

    outcome, approved_task = scheduler._run_planning_and_review(
        task, "claude", NOW, lease_nonce="test-lease"
    )

    assert outcome == "terminal"
    assert approved_task is None
    escalated = scheduler.queue.get(5)
    assert escalated.status is TaskState.NEEDS_HUMAN
    assert "exceeded workflow.limits.max_plan_revisions" in (escalated.needs_human_reason or "")


def test_characterize_plan_review_malformed_verdict_escalates(tmp_path: Path) -> None:
    worktree = tmp_path / "worktrees" / "issue-6"
    worktree.mkdir(parents=True)
    plan_file = worktree / plan_path(6)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text("# Plan\n", encoding="utf-8")

    worker = ScriptedWorker(
        {
            (6, "claude"): (
                AgentResult(AgentResultKind.PASS),
                AgentResult(AgentResultKind.PASS, output="not a valid json verdict"),
            )
        }
    )
    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=worker,
        workflow=WorkflowConfig(mode="multi-stage"),
        worktree_root=tmp_path / "worktrees",
    )

    task = Task.from_issue(Issue(number=6, title="Bad Verdict"), worktree=str(worktree))
    task = task.transition(TaskState.DISPATCHED, current_agent="claude", now=NOW)
    scheduler.queue = scheduler.queue.append([task])

    outcome, approved_task = scheduler._run_planning_and_review(
        task, "claude", NOW, lease_nonce="test-lease"
    )

    assert outcome == "terminal"
    assert approved_task is None
    escalated = scheduler.queue.get(6)
    assert escalated.status is TaskState.NEEDS_HUMAN
    assert "malformed plan review verdict" in (escalated.needs_human_reason or "")


def _init_git_repo(repo_dir: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-b", "main"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("initial\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)


def _write_review_report_file(
    worktree_dir: Path, issue_number: int, round_number: int, verdict: str
) -> None:
    from subsched.review import review_report_path

    report = review_report_path(worktree_dir, issue_number, round_number)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        f"# Review\n\n## Verdict\n{verdict}\n\n## Summary\nok\n\n## Findings\nnone\n",
        encoding="utf-8",
    )


def test_characterize_process_pr_review_approve_transition(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree-pr-1"
    worktree.mkdir(parents=True)
    _init_git_repo(worktree)
    _write_review_report_file(worktree, 1, 1, "APPROVE")

    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
    )

    task = Task(
        task_id="github-1",
        issue_number=1,
        title="PR Review Test",
        labels=(),
        status=TaskState.IN_PROGRESS,
        dispatch_status=TaskState.PR_REVIEW,
        worktree=str(worktree),
        review_cycles=0,
    )
    scheduler.queue = scheduler.queue.append([task])

    scheduler._process_pr_review(task, "claude", NOW)

    final = scheduler.queue.get(1)
    assert final.status is TaskState.READY_FOR_REVIEW
    assert final.review_cycles == 1


def test_characterize_process_pr_review_request_changes_transition(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree-pr-2"
    worktree.mkdir(parents=True)
    _init_git_repo(worktree)
    _write_review_report_file(worktree, 2, 1, "REQUEST_CHANGES")

    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
        max_review_cycles=2,
    )

    task = Task(
        task_id="github-2",
        issue_number=2,
        title="PR Review Changes",
        labels=(),
        status=TaskState.IN_PROGRESS,
        dispatch_status=TaskState.PR_REVIEW,
        worktree=str(worktree),
        review_cycles=0,
    )
    scheduler.queue = scheduler.queue.append([task])

    scheduler._process_pr_review(task, "claude", NOW)

    final = scheduler.queue.get(2)
    assert final.status is TaskState.REVISING
    assert final.review_cycles == 1


def test_characterize_process_pr_review_exceeds_max_cycles_escalates(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree-pr-3"
    worktree.mkdir(parents=True)
    _init_git_repo(worktree)
    _write_review_report_file(worktree, 3, 1, "REQUEST_CHANGES")

    store = JsonStateStore(tmp_path)
    scheduler = Scheduler(
        store=store,
        router=Router((AgentConfig("claude", 100),)),
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
        max_review_cycles=1,
    )

    task = Task(
        task_id="github-3",
        issue_number=3,
        title="Max Review Cycles",
        labels=(),
        status=TaskState.IN_PROGRESS,
        dispatch_status=TaskState.PR_REVIEW,
        worktree=str(worktree),
        review_cycles=0,
    )
    scheduler.queue = scheduler.queue.append([task])

    scheduler._process_pr_review(task, "claude", NOW)

    final = scheduler.queue.get(3)
    assert final.status is TaskState.NEEDS_HUMAN
    assert "exceeded max_review_cycles (1) with REQUEST_CHANGES verdict" in (
        final.needs_human_reason or ""
    )

