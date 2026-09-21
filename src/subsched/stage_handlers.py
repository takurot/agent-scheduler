"""Stage handlers for modular scheduler orchestration (#382).

Isolates per-stage dispatch, verdict evaluation, and state transition logic while
preserving existing external contracts, logging events, and fail-closed side-effect order.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from subsched.models import AgentResultKind, Task, TaskState
from subsched.plan_review import PlanVerdict, PlanVerdictError, parse_verdict, plan_path

if TYPE_CHECKING:
    from subsched.scheduler import Scheduler


@dataclass(frozen=True, slots=True)
class PlanReviewDecision:
    """Pure decision outcome for plan review verdict handling."""

    action: Literal["approve", "revise", "escalate"]
    new_revisions: int = 0
    reason: str | None = None


def evaluate_plan_review_verdict(
    verdict: PlanVerdict,
    current_revisions: int,
    max_revisions: int,
) -> PlanReviewDecision:
    """Pure transition decision for a plan review verdict.

    - APPROVE advances to IN_PROGRESS.
    - REQUEST_CHANGES under max_revisions loops back to PLANNING.
    - REQUEST_CHANGES at or above max_revisions escalates to NEEDS_HUMAN.
    """
    if verdict.verdict == "APPROVE":
        return PlanReviewDecision(action="approve")
    new_revisions = current_revisions + 1
    if new_revisions >= max_revisions:
        return PlanReviewDecision(
            action="escalate",
            new_revisions=new_revisions,
            reason=f"exceeded workflow.limits.max_plan_revisions ({max_revisions})",
        )
    return PlanReviewDecision(action="revise", new_revisions=new_revisions)


class PlanningStageHandler:
    """Modular handler for multi-stage planning and plan review dispatches.

    Coordinates the PLANNING -> PLAN_REVIEW -> IN_PROGRESS lifecycle loop,
    including verdict parsing, revision tracking, and fail-closed escalation.
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler

    def run(
        self,
        task: Task,
        agent: str,
        now: datetime,
        *,
        lease_nonce: str,
    ) -> tuple[Literal["approved", "terminal"], Task | None]:
        """Execute the planning and review loop for a dispatched task.

        Returns ("approved", Task) when the plan is approved and the task is transitioned
        to IN_PROGRESS, or ("terminal", None) when the task reached a terminal outcome
        (e.g., RETRY, WAITING_CAPACITY, or NEEDS_HUMAN escalation).
        """
        sched = self._scheduler
        current_task = task
        while True:
            # A REQUEST_CHANGES verdict already transitions the task PLAN_REVIEW ->
            # PLANNING at the bottom of this loop (to durably persist the incremented
            # plan_revisions counter before the next attempt starts); only transition
            # here on the loop's first iteration, when current_task is still DISPATCHED.
            stage = "planning"
            model = sched._resolve_model(agent, stage)
            effort = sched._resolve_effort(agent, stage)
            if current_task.status is TaskState.PLANNING:
                planning = current_task
            else:
                planning = current_task.transition(TaskState.PLANNING, current_agent=agent, now=now)
            planning = replace(
                planning,
                dispatch_stage=stage,
                dispatch_model=model,
                dispatch_effort=effort,
            )
            sched.queue = sched.queue.replace(planning)
            sched._persist()
            sched._log(
                "dispatch",
                issue_number=planning.issue_number,
                agent=agent,
                task_id=planning.task_id,
                data={
                    "attempt": planning.attempt,
                    "stage": stage,
                    "agent": agent,
                    "model": model or "provider-default",
                    "effort": effort or "provider-default",
                },
            )
            result = sched._run_stage_worker(planning, agent, lease_nonce)
            if result.kind is not AgentResultKind.PASS:
                sched._handle_result(planning, agent, result, now)
                return "terminal", None

            plan_file_exists = (
                planning.worktree is not None
                and (Path(planning.worktree) / plan_path(planning.issue_number)).is_file()
            )
            if not plan_file_exists:
                sched._escalate_stage(
                    planning,
                    agent,
                    now,
                    "planning stage completed without producing a plan file",
                )
                return "terminal", None

            if not sched.workflow.stages.plan_review:
                # #280: plan_review is opt-out -- an existing plan file is enough to
                # auto-approve and proceed straight to implementation.
                approved = planning.transition(TaskState.PLAN_REVIEW, current_agent=agent, now=now)
                approved = approved.transition(TaskState.IN_PROGRESS, current_agent=agent, now=now)
                approved = replace(approved, plan_approved=True)
                sched.queue = sched.queue.replace(approved)
                sched._persist()
                return "approved", approved

            stage = "plan_review"
            model = sched._resolve_model(agent, stage)
            effort = sched._resolve_effort(agent, stage)
            review = planning.transition(TaskState.PLAN_REVIEW, current_agent=agent, now=now)
            review = replace(
                review,
                dispatch_stage=stage,
                dispatch_model=model,
                dispatch_effort=effort,
            )
            sched.queue = sched.queue.replace(review)
            sched._persist()
            sched._log(
                "dispatch",
                issue_number=review.issue_number,
                agent=agent,
                task_id=review.task_id,
                data={
                    "attempt": review.attempt,
                    "stage": stage,
                    "agent": agent,
                    "model": model or "provider-default",
                    "effort": effort or "provider-default",
                },
            )
            review_result = sched._run_stage_worker(review, agent, lease_nonce)
            if review_result.kind is not AgentResultKind.PASS:
                sched._handle_result(review, agent, review_result, now)
                return "terminal", None

            verdict = review_result.plan_verdict
            if verdict is None:
                try:
                    verdict = parse_verdict(review_result.output)
                except PlanVerdictError as error:
                    sched._escalate_stage(
                        review, agent, now, f"malformed plan review verdict: {error}"
                    )
                    return "terminal", None

            decision = evaluate_plan_review_verdict(
                verdict, review.plan_revisions, sched.max_plan_revisions
            )

            if decision.action == "approve":
                approved = review.transition(TaskState.IN_PROGRESS, current_agent=agent, now=now)
                approved = replace(approved, plan_approved=True)
                sched.queue = sched.queue.replace(approved)
                sched._persist()
                sched._log(
                    "task_transition",
                    issue_number=approved.issue_number,
                    agent=agent,
                    task_id=approved.task_id,
                    data={
                        "from_state": review.status.value,
                        "to_state": approved.status.value,
                        "verdict": verdict.verdict,
                    },
                )
                return "approved", approved

            if decision.action == "escalate":
                escalated = replace(review, plan_revisions=decision.new_revisions)
                sched._escalate_stage(
                    escalated,
                    agent,
                    now,
                    decision.reason
                    or f"exceeded workflow.limits.max_plan_revisions ({sched.max_plan_revisions})",
                )
                return "terminal", None

            # decision.action == "revise"
            back_to_planning = review.transition(TaskState.PLANNING, current_agent=agent, now=now)
            back_to_planning = replace(back_to_planning, plan_revisions=decision.new_revisions)
            sched.queue = sched.queue.replace(back_to_planning)
            sched._persist()
            sched._log(
                "task_transition",
                issue_number=back_to_planning.issue_number,
                agent=agent,
                task_id=back_to_planning.task_id,
                data={
                    "from_state": review.status.value,
                    "to_state": back_to_planning.status.value,
                    "verdict": verdict.verdict,
                    "plan_revisions": decision.new_revisions,
                },
            )
            current_task = back_to_planning
