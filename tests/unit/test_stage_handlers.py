"""Unit tests for modular stage handlers (#382)."""

from __future__ import annotations

from subsched.plan_review import PlanVerdict
from subsched.stage_handlers import (
    PlanReviewDecision,
    evaluate_plan_review_verdict,
)


def test_evaluate_plan_review_verdict_approve() -> None:
    verdict = PlanVerdict(verdict="APPROVE", summary="Looks good", findings=())
    decision = evaluate_plan_review_verdict(verdict, current_revisions=0, max_revisions=2)
    assert decision == PlanReviewDecision(action="approve")


def test_evaluate_plan_review_verdict_request_changes_within_limit() -> None:
    verdict = PlanVerdict(verdict="REQUEST_CHANGES", summary="Fix issues", findings=())
    decision = evaluate_plan_review_verdict(verdict, current_revisions=0, max_revisions=2)
    assert decision == PlanReviewDecision(action="revise", new_revisions=1)


def test_evaluate_plan_review_verdict_request_changes_reaches_limit() -> None:
    verdict = PlanVerdict(verdict="REQUEST_CHANGES", summary="Fix issues", findings=())
    decision = evaluate_plan_review_verdict(verdict, current_revisions=1, max_revisions=2)
    assert decision.action == "escalate"
    assert decision.new_revisions == 2
    assert "exceeded workflow.limits.max_plan_revisions (2)" in (decision.reason or "")
