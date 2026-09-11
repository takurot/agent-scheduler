from __future__ import annotations

from subsched.models import Issue, Task, TaskState
from subsched.plan_review import PlanVerdict, PlanVerdictError, parse_verdict, plan_path


def test_plan_path_is_relative_and_stable() -> None:
    assert plan_path(280) == plan_path(280)
    assert str(plan_path(280)) == ".ai/plans/280.md"


def test_parse_verdict_approve() -> None:
    verdict = parse_verdict('{"verdict": "APPROVE", "summary": "looks good", "findings": []}')
    assert verdict == PlanVerdict(verdict="APPROVE", summary="looks good", findings=())


def test_parse_verdict_request_changes_with_findings() -> None:
    verdict = parse_verdict(
        '{"verdict": "REQUEST_CHANGES", "summary": "missing tests", '
        '"findings": ["no test for the error path"]}'
    )
    assert verdict.verdict == "REQUEST_CHANGES"
    assert verdict.findings == ("no test for the error path",)


def test_parse_verdict_rejects_malformed_json() -> None:
    import pytest

    with pytest.raises(PlanVerdictError, match="not valid JSON"):
        parse_verdict("not json at all")


def test_parse_verdict_rejects_unsupported_verdict_value() -> None:
    import pytest

    with pytest.raises(PlanVerdictError, match="unsupported or missing"):
        parse_verdict('{"verdict": "MAYBE"}')


def test_parse_verdict_rejects_non_object_payload() -> None:
    import pytest

    with pytest.raises(PlanVerdictError, match="must be a JSON object"):
        parse_verdict("[1, 2, 3]")


def test_parse_verdict_rejects_empty_output() -> None:
    import pytest

    with pytest.raises(PlanVerdictError, match="empty"):
        parse_verdict("")


def test_task_plan_revisions_and_plan_approved_round_trip_to_dict() -> None:
    task = Task.from_issue(Issue(number=280, title="multi-stage workflow"))
    task = task.transition(TaskState.DISPATCHED)
    task = task.transition(TaskState.PLANNING)
    task = task.transition(TaskState.PLAN_REVIEW)
    from dataclasses import replace

    task = replace(task, plan_revisions=1)
    payload = task.to_dict()
    assert payload["plan_revisions"] == 1
    assert payload["plan_approved"] is False

    restored = Task.from_dict(payload)
    assert restored.plan_revisions == 1
    assert restored.plan_approved is False
    assert restored.status is TaskState.PLAN_REVIEW


def test_task_plan_fields_default_and_persist_when_approved() -> None:
    task = Task.from_issue(Issue(number=281, title="multi-stage workflow"))
    assert task.plan_revisions == 0
    assert task.plan_approved is False

    from dataclasses import replace

    approved = replace(task, plan_approved=True)
    restored = Task.from_dict(approved.to_dict())
    assert restored.plan_approved is True
