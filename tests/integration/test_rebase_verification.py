from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from subsched.checkpoint import load_checkpoint
from subsched.github.pull_requests import PullRequestInfo, PullRequestResult, PullRequestResultKind
from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue, TaskState
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler
from subsched.storage import JsonStateStore
from subsched.tasks.worktree import GitWorktreeAdapter


def _available(agent: str) -> Capacity:
    return Capacity(
        agent=agent,
        state=CapacityState.AVAILABLE,
        observed_at=datetime.now(UTC),
        source="provider",
        confidence="high",
    )


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], text=True, capture_output=True, check=True)
    return r.stdout.strip()


def _init_repo(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    _git(p, "init", "-b", "main")
    _git(p, "config", "user.name", "Test User")
    _git(p, "config", "user.email", "test@example.invalid")


def test_scheduler_fails_and_does_not_push_on_post_rebase_verification_failure(
    tmp_path: Path,
) -> None:
    """#202: When remote base changes and a clean rebase causes a verification failure,
    the scheduler must rerun verification, fail before push/PR, and record the checkpoint
    against the rebased commit.
    """
    seed = tmp_path / "seed"
    _init_repo(seed)
    (seed / ".gitignore").write_text(".ai/\n", encoding="utf-8")
    (seed / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "Initial commit")

    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(remote)], check=True, capture_output=True
    )
    _git(seed, "remote", "add", "origin", str(remote))

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.name", "Test User")
    _git(clone, "config", "user.email", "test@example.invalid")

    class Worker:
        def run(self, task, agent):
            wt = Path(task.worktree)
            (wt / "check.py").write_text(
                "from config import VALUE\nassert VALUE == 1, f'Expected 1, got {VALUE}'\n",
                encoding="utf-8",
            )
            _git(wt, "add", "check.py")
            _git(wt, "commit", "-m", "Add task check")

            # Remote base moves while the task works (semantic conflict)
            (seed / "config.py").write_text("VALUE = 200\n", encoding="utf-8")
            _git(seed, "add", "config.py")
            _git(seed, "commit", "-m", "Change base contract")
            _git(seed, "push", "origin", "main")
            return AgentResult(AgentResultKind.PASS)

    s = Scheduler(
        store=JsonStateStore(clone),
        router=Router([AgentConfig("claude", 100)]),
        worker=Worker(),
        worktree_root=clone / ".ai" / "worktrees",
        worktree_adapter=GitWorktreeAdapter(clone, clone / ".ai" / "worktrees"),
        verification_commands=(f"{sys.executable} check.py",),
        push_enabled=True,
        create_pr_enabled=True,
        repo="audit/fixture",
        base_branch="main",
    )
    s.discover((Issue(1, "Add task check"),))

    fake_pr = PullRequestResult(
        PullRequestResultKind.SUCCESS,
        PullRequestInfo(9, "https://example.invalid/pr/9", "fixture", "fixture"),
    )
    with patch(
        "subsched.github.pull_requests.create_or_get_pull_request", return_value=fake_pr
    ) as create_mock:
        s.tick([_available("claude")])

    # PR creation must NOT be called because post-rebase verification failed
    assert create_mock.call_count == 0

    # Remote origin must NOT have the branch pushed
    proc = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "--verify", "refs/heads/subsched/issue-1"],
        capture_output=True,
    )
    assert proc.returncode != 0, "Branch must not be pushed on verification failure"

    # Task status must be RETRY/READY or NEEDS_HUMAN, not READY_FOR_REVIEW
    task = s.tasks[0]
    assert task.status is not TaskState.READY_FOR_REVIEW
    assert task.status in {TaskState.READY, TaskState.NEEDS_HUMAN}

    # Checkpoint must reflect the post-rebase commit and failure
    wt = Path(task.worktree)
    rebased_head = _git(wt, "rev-parse", "HEAD")
    cp = load_checkpoint(wt, 1)
    assert cp is not None
    assert cp.head_commit == rebased_head
    assert cp.exit_code != 0


def test_scheduler_pushes_when_post_rebase_verification_passes(tmp_path: Path) -> None:
    """When remote base changes cleanly and post-rebase verification passes,
    the scheduler pushes the rebased branch and creates a PR with the updated checkpoint.
    """
    seed = tmp_path / "seed"
    _init_repo(seed)
    (seed / ".gitignore").write_text(".ai/\n", encoding="utf-8")
    (seed / "config.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "Initial commit")

    remote = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(remote)], check=True, capture_output=True
    )
    _git(seed, "remote", "add", "origin", str(remote))

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(remote), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.name", "Test User")
    _git(clone, "config", "user.email", "test@example.invalid")

    class Worker:
        def run(self, task, agent):
            wt = Path(task.worktree)
            (wt / "check.py").write_text(
                "from config import VALUE\nassert VALUE == 1\n",
                encoding="utf-8",
            )
            _git(wt, "add", "check.py")
            _git(wt, "commit", "-m", "Add task check")

            # Remote base moves but does NOT conflict with check.py
            (seed / "OTHER.md").write_text("Independent change\n", encoding="utf-8")
            _git(seed, "add", "OTHER.md")
            _git(seed, "commit", "-m", "Unrelated base change")
            _git(seed, "push", "origin", "main")
            return AgentResult(AgentResultKind.PASS)

    s = Scheduler(
        store=JsonStateStore(clone),
        router=Router([AgentConfig("claude", 100)]),
        worker=Worker(),
        worktree_root=clone / ".ai" / "worktrees",
        worktree_adapter=GitWorktreeAdapter(clone, clone / ".ai" / "worktrees"),
        verification_commands=(f"{sys.executable} check.py",),
        push_enabled=True,
        create_pr_enabled=True,
        repo="audit/fixture",
        base_branch="main",
    )
    s.discover((Issue(1, "Add task check"),))

    fake_pr = PullRequestResult(
        PullRequestResultKind.SUCCESS,
        PullRequestInfo(9, "https://example.invalid/pr/9", "fixture", "fixture"),
    )
    with patch(
        "subsched.github.pull_requests.create_or_get_pull_request", return_value=fake_pr
    ) as create_mock:
        s.tick([_available("claude")])


    assert create_mock.call_count == 1

    proc = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-parse", "--verify", "refs/heads/subsched/issue-1"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    pushed_head = proc.stdout.strip()

    task = s.tasks[0]
    assert task.status is TaskState.READY_FOR_REVIEW

    wt = Path(task.worktree)
    rebased_head = _git(wt, "rev-parse", "HEAD")
    assert pushed_head == rebased_head

    cp = load_checkpoint(wt, 1)
    assert cp is not None
    assert cp.head_commit == rebased_head
    assert cp.exit_code == 0
