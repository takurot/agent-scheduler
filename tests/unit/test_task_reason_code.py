from __future__ import annotations

import pytest

from subsched.models import (
    NEEDS_HUMAN_REASON_CODES,
    Task,
    TaskState,
)


def _make_task(status: TaskState = TaskState.READY, **kwargs) -> Task:
    defaults = dict(
        task_id="task-1",
        issue_number=1,
        title="Test task",
        labels=(),
        status=status,
    )
    defaults.update(kwargs)
    return Task(**defaults)


def test_task_needs_human_reason_code_default_none() -> None:
    task = _make_task()
    assert task.needs_human_reason_code is None


def test_task_needs_human_reason_code_valid_enums() -> None:
    for code in NEEDS_HUMAN_REASON_CODES:
        task = _make_task(
            status=TaskState.NEEDS_HUMAN,
            needs_human_reason="blocked",
            needs_human_reason_code=code,
        )
        assert task.needs_human_reason_code == code


def test_task_needs_human_reason_code_invalid_raises() -> None:
    with pytest.raises(ValueError, match="invalid needs_human_reason_code"):
        _make_task(
            status=TaskState.NEEDS_HUMAN,
            needs_human_reason="blocked",
            needs_human_reason_code="invalid_reason",
        )


def test_task_transition_to_needs_human_with_reason_code() -> None:
    task = _make_task(status=TaskState.IN_PROGRESS)
    transitioned = task.transition(
        TaskState.NEEDS_HUMAN,
        reason="operator intervention required",
        reason_code="operator_decision_required",
    )
    assert transitioned.status is TaskState.NEEDS_HUMAN
    assert transitioned.needs_human_reason == "operator intervention required"
    assert transitioned.needs_human_reason_code == "operator_decision_required"


def test_task_transition_clears_reason_code_when_leaving_needs_human() -> None:
    task = _make_task(
        status=TaskState.NEEDS_HUMAN,
        needs_human_reason="waiting",
        needs_human_reason_code="operator_decision_required",
    )
    # Re-queue / resolve to READY
    transitioned = task.transition(TaskState.READY)
    assert transitioned.status is TaskState.READY
    assert transitioned.needs_human_reason is None
    assert transitioned.needs_human_reason_code is None


def test_task_serialization_round_trip() -> None:
    task = _make_task(
        status=TaskState.NEEDS_HUMAN,
        needs_human_reason="conflict",
        needs_human_reason_code="instruction_conflict",
    )
    data = task.to_dict()
    assert data["needs_human_reason_code"] == "instruction_conflict"

    restored = Task.from_dict(data)
    assert restored.needs_human_reason_code == "instruction_conflict"
    assert restored.needs_human_reason == "conflict"


def test_task_from_dict_backward_compatible_without_reason_code() -> None:
    task = _make_task(status=TaskState.NEEDS_HUMAN, needs_human_reason="old format")
    data = task.to_dict()
    # Simulate pre-#300 persisted JSON state lacking needs_human_reason_code
    data.pop("needs_human_reason_code", None)

    restored = Task.from_dict(data)
    assert restored.needs_human_reason_code is None
    assert restored.needs_human_reason == "old format"
