from datetime import UTC, datetime, timedelta

import pytest

from subsched.models import (
    AgentResult,
    AgentResultKind,
    Capacity,
    CapacityState,
    Issue,
    StateTransitionError,
    Task,
    TaskState,
)


def test_issue_maps_to_immutable_ready_task() -> None:
    issue = Issue(number=103, title="Timeout", body="Blocked-By: #101", labels=("bug",))

    task = Task.from_issue(issue, worktree="/tmp/issue-103")

    assert task.task_id == "github-103"
    assert task.status is TaskState.WAITING_DEPENDENCY
    assert task.dependencies == (101,)
    assert task.worktree == "/tmp/issue-103"
    with pytest.raises(AttributeError):
        task.title = "changed"  # type: ignore[misc]


def test_task_transition_rejects_terminal_state_restart() -> None:
    task = Task.from_issue(Issue(number=1, title="one"))
    completed = task.transition(TaskState.DISPATCHED).transition(TaskState.IN_PROGRESS)
    completed = completed.transition(TaskState.COMPLETE)

    with pytest.raises(StateTransitionError):
        completed.transition(TaskState.IN_PROGRESS)


def test_capacity_reports_availability_and_remaining_percentage() -> None:
    now = datetime.now(UTC)
    capacity = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        used_percentage=35.5,
        reset_at=now + timedelta(hours=1),
        observed_at=now,
        source="provider",
        confidence="high",
    )

    assert capacity.is_available(now)
    assert capacity.remaining_percentage == 64.5


@pytest.mark.parametrize(
    "state",
    [
        CapacityState.DISABLED_BILLING,
        CapacityState.AUTH_ERROR,
        CapacityState.UNKNOWN,
        CapacityState.FAILED,
        CapacityState.COOLDOWN_SESSION,
    ],
)
def test_non_available_capacity_requires_a_fresh_probe(state: CapacityState) -> None:
    now = datetime.now(UTC)
    capacity = Capacity(
        agent="claude",
        state=state,
        reset_at=now - timedelta(seconds=1),
        observed_at=now,
        source="provider",
        confidence="high",
    )

    assert capacity.is_available(now) is False


def test_capacity_result_requires_reset_for_capacity_event() -> None:
    with pytest.raises(ValueError, match="reset_at"):
        AgentResult(kind=AgentResultKind.CAPACITY_SESSION)
