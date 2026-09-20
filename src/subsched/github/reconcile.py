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
import shutil
import subprocess
from collections.abc import Iterable, Mapping, Sequence
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
    # #377: tracked PRs left unfetched because of the per-run request cap (unknown, so
    # UNCHANGED). Surfaced so an operator can see that a run was not exhaustive.
    deferred: tuple[int, ...] = ()


# #377: upper bound on `gh pr view` calls per reconcile run (rate-limit guard). PRs
# beyond the cap are left unknown -- and therefore UNCHANGED -- and reported as deferred.
# Order is ascending, so long-lived OPEN low-numbered PRs are re-checked first each run;
# a backlog of >= cap open PRs can therefore defer higher-numbered ones until some of
# them resolve.
DEFAULT_MAX_PR_REQUESTS = 100
_MAX_PR_NUMBER = 2**31 - 1


def _failure(error: str) -> PrLifecycleFetchResult:
    return PrLifecycleFetchResult(kind=PrLifecycleFetchKind.FAILURE, states={}, error=error)


def _fetch_one_pr_state(
    repo: str,
    number: int,
    env: dict[str, str] | None,
    timeout_seconds: float,
) -> tuple[PrLifecycleState | None, str]:
    """Return `(state, "")` for one PR, or `(None, error)` (fail closed)."""
    argv = ["gh", "pr", "view", str(number), "--repo", repo, "--json", "number,state"]
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
        return None, _redact(f"gh pr view invocation failed for PR #{number}: {error}")
    if res.returncode != 0:
        return None, _redact(
            f"gh pr view exited {res.returncode} for PR #{number}: {res.stderr.strip()}"
        )
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None, f"unparseable gh pr view output for PR #{number}"
    if not isinstance(data, dict):
        return None, f"invalid gh pr view output structure for PR #{number}"
    returned_number = data.get("number")
    if type(returned_number) is not int or "state" not in data:
        return None, f"malformed gh pr view output for PR #{number}"
    if returned_number != number:
        return (
            None,
            f"gh pr view returned PR #{returned_number} which does not match PR #{number}",
        )
    raw_state = str(data["state"]).upper()
    try:
        return PrLifecycleState(raw_state), ""
    except ValueError:
        return None, f"unrecognized PR state '{raw_state}' for PR #{number}"


def fetch_pr_lifecycle_states(
    repo: str,
    pr_numbers: Iterable[int],
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
    max_requests: int = DEFAULT_MAX_PR_REQUESTS,
) -> PrLifecycleFetchResult:
    """Fetch the lifecycle state of each tracked PR with one `gh pr view` call apiece.

    Querying the tracked PR numbers directly (rather than a fixed window of recent
    PRs) means an old PR is never silently missed (#377). PR numbers are deduplicated
    and processed in ascending order; at most `max_requests` calls are made per
    invocation.

    Fails closed (FAILURE, empty states) if any call errors, times out, exits non-zero,
    or returns malformed / mismatching JSON -- callers must not mutate scheduler state
    in that case. A PR beyond `max_requests` is simply absent from `states`; callers
    treat that as "unknown", not as a specific lifecycle state.
    """
    numbers = list(pr_numbers)
    for number in numbers:
        # Persisted state is untrusted: only positive plain ints may reach gh's argv
        # (a value like "--web" or -5 would otherwise be parsed as a flag).
        if type(number) is not int or not 0 < number <= _MAX_PR_NUMBER:
            return _failure(f"invalid tracked PR number: {number!r}")
    ordered = sorted(set(numbers))
    cap = max(max_requests, 0)
    states: dict[int, PrLifecycleState] = {}
    for number in ordered[:cap]:
        state, error = _fetch_one_pr_state(repo, number, env, timeout_seconds)
        if state is None:
            return _failure(error)
        states[number] = state
    return PrLifecycleFetchResult(
        kind=PrLifecycleFetchKind.SUCCESS, states=states, deferred=tuple(ordered[cap:])
    )


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

    A task whose PR number is absent from `pr_states` (e.g. beyond the
    per-run request cap) is left UNCHANGED -- an unknown PR state is never treated as
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
                else f"PR #{task.pr} state was not fetched or could not be determined"
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
    - `git status --porcelain -uall` for the worktree reports no staged or unstaged
      changes and no untracked files outside the Scheduler-owned `.ai/` directory.

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
            ["git", "-C", str(resolved_path), "status", "--porcelain=v1", "-z", "-uall"],
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
    for entry in status.stdout.split("\0"):
        if not entry:
            continue
        if len(entry) < 4 or entry[2] != " ":
            unsafe_change = True
        else:
            status_code = entry[:2]
            path_parts = entry[3:].split("/")
            unsafe_change = (
                status_code != "??"
                or len(path_parts) < 2
                or path_parts[0] != ".ai"
                or any(part in {"", ".", ".."} for part in path_parts)
            )
        if unsafe_change:
            return WorktreePruneResult(
                kind=WorktreePruneKind.SKIPPED,
                reason=(
                    f"worktree {worktree_path} has uncommitted or untracked changes; "
                    "refusing to prune"
                ),
            )

    ai_dir = resolved_path / ".ai"
    if ai_dir.exists():
        if ai_dir.is_symlink():
            return WorktreePruneResult(
                kind=WorktreePruneKind.FAILED,
                reason=f"refusing to prune worktree with symlinked .ai directory: {ai_dir}",
            )
        try:
            shutil.rmtree(ai_dir)
        except OSError as error:
            return WorktreePruneResult(
                kind=WorktreePruneKind.FAILED,
                reason=_redact(f"could not clean scheduler state before prune: {error}"),
            )

    try:
        removal = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "worktree",
                "remove",
                str(resolved_path),
            ],
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
