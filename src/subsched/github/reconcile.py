"""Reconciliation of `READY_FOR_REVIEW` tasks against actual GitHub PR state (#277).

Under the default configuration (`execution.ci_monitoring: false`), the Scheduler
never revisits a task once its PR is created -- `READY_FOR_REVIEW` is a durable
"awaiting human review" state (docs/SPEC.md's COMPLETE definition, #142). Once a
maintainer merges (or closes without merging) that PR on GitHub, nothing in the
normal dispatch loop notices, so completed work accumulates indefinitely under
`READY_FOR_REVIEW`. `subsched reconcile` closes that gap by explicitly querying `gh`
for the current state of every tracked PR and advancing tasks accordingly -- it is
never invoked implicitly by `discover()` or the dispatch loop.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from subsched.agents.process import redact_sensitive_command_audit
from subsched.models import Task, TaskState


def _redact(text: str) -> str:
    return "\n".join(redact_sensitive_command_audit(tuple(text.splitlines())))


class PrLifecycleState(StrEnum):
    OPEN = "OPEN"
    MERGED = "MERGED"
    CLOSED = "CLOSED"


class PrLifecycleFetchKind(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class PrLifecycleFetchResult:
    kind: PrLifecycleFetchKind
    states: Mapping[int, PrLifecycleState]
    error: str = ""


def fetch_pr_lifecycle_states(
    repo: str,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
    limit: int = 100,
) -> PrLifecycleFetchResult:
    """Batch-fetch PR number -> lifecycle state via a single `gh pr list` call.

    Fails closed (FAILURE, empty states) on any `gh` invocation error, non-zero exit,
    or malformed JSON -- callers must not mutate scheduler state in that case. A PR
    number absent from the result (e.g. older than `limit`) is simply not present in
    `states`; callers treat that as "unknown", not as a specific lifecycle state.
    """
    argv = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "all",
        "--limit",
        str(limit),
        "--json",
        "number,state,mergedAt",
    ]
    try:
        res = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return PrLifecycleFetchResult(
            kind=PrLifecycleFetchKind.FAILURE,
            states={},
            error=_redact(f"gh pr list invocation failed: {error}"),
        )
    if res.returncode != 0:
        return PrLifecycleFetchResult(
            kind=PrLifecycleFetchKind.FAILURE,
            states={},
            error=_redact(
                f"gh pr list exited {res.returncode}: {res.stderr.strip()}"
            ),
        )
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return PrLifecycleFetchResult(
            kind=PrLifecycleFetchKind.FAILURE,
            states={},
            error="unparseable gh pr list output",
        )
    if not isinstance(data, list):
        return PrLifecycleFetchResult(
            kind=PrLifecycleFetchKind.FAILURE,
            states={},
            error="invalid gh pr list output structure",
        )

    states: dict[int, PrLifecycleState] = {}
    for item in data:
        if not isinstance(item, dict):
            return PrLifecycleFetchResult(
                kind=PrLifecycleFetchKind.FAILURE,
                states={},
                error="malformed entry in gh pr list output",
            )
        try:
            number = int(item["number"])
            raw_state = str(item["state"]).upper()
        except (KeyError, TypeError, ValueError):
            return PrLifecycleFetchResult(
                kind=PrLifecycleFetchKind.FAILURE,
                states={},
                error="malformed entry in gh pr list output",
            )
        if raw_state == "MERGED":
            states[number] = PrLifecycleState.MERGED
        elif raw_state == "CLOSED":
            states[number] = PrLifecycleState.CLOSED
        elif raw_state == "OPEN":
            states[number] = PrLifecycleState.OPEN
        else:
            return PrLifecycleFetchResult(
                kind=PrLifecycleFetchKind.FAILURE,
                states={},
                error=f"unrecognized PR state '{raw_state}' for PR #{number}",
            )

    return PrLifecycleFetchResult(kind=PrLifecycleFetchKind.SUCCESS, states=states)


class ReconcileAction(StrEnum):
    COMPLETE = "COMPLETE"
    NEEDS_HUMAN = "NEEDS_HUMAN"
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True, slots=True)
class ReconcilePlanItem:
    issue_number: int
    pr: int
    from_status: TaskState
    action: ReconcileAction
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    updated_tasks: tuple[Task, ...]
    items: tuple[ReconcilePlanItem, ...]

    @property
    def reconciled_complete(self) -> int:
        return sum(1 for item in self.items if item.action is ReconcileAction.COMPLETE)

    @property
    def reconciled_needs_human(self) -> int:
        return sum(1 for item in self.items if item.action is ReconcileAction.NEEDS_HUMAN)

    @property
    def unchanged(self) -> int:
        return sum(1 for item in self.items if item.action is ReconcileAction.UNCHANGED)


def plan_reconciliation(
    tasks: Sequence[Task],
    pr_states: Mapping[int, PrLifecycleState],
    *,
    now: datetime | None = None,
) -> ReconcileResult:
    """Compute (but do not persist) state transitions for `READY_FOR_REVIEW` tasks
    with an associated PR, based on `pr_states` fetched from GitHub.

    A task whose PR number is absent from `pr_states` (e.g. older than the batch
    query's `limit`) is left UNCHANGED -- an unknown PR state is never treated as
    "still open" or "merged" (fail closed).
    """
    current = now or datetime.now(UTC)
    updated: list[Task] = []
    items: list[ReconcilePlanItem] = []
    for task in tasks:
        if task.status is not TaskState.READY_FOR_REVIEW or task.pr is None:
            updated.append(task)
            continue

        state = pr_states.get(task.pr)
        if state is PrLifecycleState.MERGED:
            new_task = task.transition(TaskState.COMPLETE, now=current)
            items.append(
                ReconcilePlanItem(
                    issue_number=task.issue_number,
                    pr=task.pr,
                    from_status=task.status,
                    action=ReconcileAction.COMPLETE,
                    reason=f"PR #{task.pr} was merged",
                )
            )
            updated.append(new_task)
        elif state is PrLifecycleState.CLOSED:
            reason = f"PR #{task.pr} was closed without being merged"
            new_task = task.transition(TaskState.NEEDS_HUMAN, now=current, reason=reason)
            items.append(
                ReconcilePlanItem(
                    issue_number=task.issue_number,
                    pr=task.pr,
                    from_status=task.status,
                    action=ReconcileAction.NEEDS_HUMAN,
                    reason=reason,
                )
            )
            updated.append(new_task)
        else:
            # OPEN or unknown (absent from the batch query): remain in READY_FOR_REVIEW.
            reason = (
                "PR is still open"
                if state is PrLifecycleState.OPEN
                else f"PR #{task.pr} state could not be determined from gh output"
            )
            items.append(
                ReconcilePlanItem(
                    issue_number=task.issue_number,
                    pr=task.pr,
                    from_status=task.status,
                    action=ReconcileAction.UNCHANGED,
                    reason=reason,
                )
            )
            updated.append(task)

    return ReconcileResult(updated_tasks=tuple(updated), items=tuple(items))


class WorktreePruneKind(StrEnum):
    PRUNED = "PRUNED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class WorktreePruneResult:
    kind: WorktreePruneKind
    reason: str = ""


def prune_worktree_if_clean(
    repo_root: Path,
    worktree_root: Path,
    issue_number: int,
    worktree_path: Path,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> WorktreePruneResult:
    """Remove a task's worktree after it has reconciled to COMPLETE, but only when
    it is unambiguously safe to do so. Never runs by default -- callers must pass
    `--prune-worktrees` explicitly. Refuses (SKIPPED, never FAILED) unless *all* of:

    - `worktree_path` resolves inside `worktree_root` (never escapes repo boundaries),
      is not a symlink, and matches the Scheduler's own naming convention for this
      issue number.
    - `git status --porcelain` for the worktree reports no changes at all (staged,
      unstaged, *or* untracked).

    Uses `git worktree remove` (never `rm -rf`), so git's own worktree registry stays
    consistent; a worktree git itself refuses to remove is reported as FAILED.
    """
    resolved_root = worktree_root.resolve()
    expected_path = (worktree_root / f"issue-{issue_number}").resolve()

    if worktree_path.is_symlink():
        return WorktreePruneResult(
            kind=WorktreePruneKind.SKIPPED,
            reason=f"worktree path {worktree_path} is a symlink; refusing to prune",
        )
    resolved_path = worktree_path.resolve()
    if resolved_path != expected_path or not resolved_path.is_relative_to(resolved_root):
        return WorktreePruneResult(
            kind=WorktreePruneKind.SKIPPED,
            reason=(
                f"worktree path {worktree_path} does not match the expected location "
                f"for issue #{issue_number}; refusing to prune"
            ),
        )
    if not resolved_path.is_dir():
        return WorktreePruneResult(
            kind=WorktreePruneKind.SKIPPED,
            reason=f"worktree path {worktree_path} does not exist; nothing to prune",
        )

    try:
        status = subprocess.run(
            ["git", "-C", str(resolved_path), "status", "--porcelain"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return WorktreePruneResult(
            kind=WorktreePruneKind.FAILED,
            reason=_redact(f"could not check worktree status: {error}"),
        )
    if status.returncode != 0:
        return WorktreePruneResult(
            kind=WorktreePruneKind.FAILED,
            reason=_redact(f"git status failed: {status.stderr.strip()}"),
        )
    if status.stdout.strip():
        return WorktreePruneResult(
            kind=WorktreePruneKind.SKIPPED,
            reason=(
                f"worktree {worktree_path} has uncommitted or untracked changes; "
                "refusing to prune"
            ),
        )

    try:
        removal = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "remove", str(resolved_path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return WorktreePruneResult(
            kind=WorktreePruneKind.FAILED,
            reason=_redact(f"could not remove worktree: {error}"),
        )
    if removal.returncode != 0:
        return WorktreePruneResult(
            kind=WorktreePruneKind.FAILED,
            reason=_redact(f"git worktree remove failed: {removal.stderr.strip()}"),
        )
    return WorktreePruneResult(kind=WorktreePruneKind.PRUNED)
