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
    running = task.transition(TaskState.DISPATCHED).transition(TaskState.IN_PROGRESS)
    verifying = running.transition(TaskState.VERIFYING)
    pr_ready = verifying.transition(TaskState.PR_READY)
    ready_review = pr_ready.transition(TaskState.READY_FOR_REVIEW)
    completed = ready_review.transition(TaskState.COMPLETE)

    with pytest.raises(StateTransitionError):
        completed.transition(TaskState.IN_PROGRESS)


def test_in_progress_cannot_transition_directly_to_complete() -> None:
    task = Task.from_issue(Issue(number=1, title="one"))
    running = task.transition(TaskState.DISPATCHED).transition(TaskState.IN_PROGRESS)
    with pytest.raises(StateTransitionError, match="invalid transition"):
        running.transition(TaskState.COMPLETE)


@pytest.mark.parametrize("from_state", list(TaskState))
def test_all_state_transitions_match_allowed_matrix(from_state: TaskState) -> None:
    from subsched.models import ALLOWED_TRANSITIONS

    dummy_task = Task(
        task_id="github-1",
        issue_number=1,
        title="test",
        labels=(),
        status=from_state,
    )
    allowed = ALLOWED_TRANSITIONS[from_state]

    for to_state in TaskState:
        if to_state in allowed:
            next_task = dummy_task.transition(to_state)
            assert next_task.status is to_state
        else:
            with pytest.raises(StateTransitionError):
                dummy_task.transition(to_state)


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


def test_self_dependency_creates_blocked_task() -> None:
    issue = Issue(number=101, title="Self blocked", body="Blocked-By: #101")
    task = Task.from_issue(issue)
    assert task.status is TaskState.BLOCKED
    assert task.dependencies == (101,)


def test_detect_dependency_cycles() -> None:
    from subsched.models import detect_dependency_cycles

    t1 = Task(
        task_id="github-1",
        issue_number=1,
        title="1",
        labels=(),
        status=TaskState.WAITING_DEPENDENCY,
        dependencies=(2,),
    )
    t2 = Task(
        task_id="github-2",
        issue_number=2,
        title="2",
        labels=(),
        status=TaskState.WAITING_DEPENDENCY,
        dependencies=(3,),
    )
    t3 = Task(
        task_id="github-3",
        issue_number=3,
        title="3",
        labels=(),
        status=TaskState.WAITING_DEPENDENCY,
        dependencies=(1,),
    )
    t4 = Task(
        task_id="github-4",
        issue_number=4,
        title="4",
        labels=(),
        status=TaskState.READY,
        dependencies=(),
    )

    cycles = detect_dependency_cycles((t1, t2, t3, t4))
    assert cycles == {1, 2, 3}
