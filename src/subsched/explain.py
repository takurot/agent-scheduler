"""Read-only explanation of issue eligibility, queue, dependencies, and capacity."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from subsched.config import SchedulerConfig
from subsched.models import (
    Capacity,
    Issue,
    Task,
    TaskState,
    detect_dependency_cycles,
    parse_dependencies,
)
from subsched.router import AgentConfig, Router, _is_fresh_provider
from subsched.selection import excluded_issue_labels


def explain_issue(
    issue: Issue,
    tasks: Iterable[Task],
    config: SchedulerConfig,
    capacities: Iterable[Capacity],
    *,
    now: datetime | None = None,
    run_dispatched_issues: set[int] | frozenset[int] | None = None,
) -> dict[str, Any]:
    current = now or datetime.now(UTC)
    task_map = {t.issue_number: t for t in tasks}
    task = task_map.get(issue.number)

    reason_codes: list[str] = []
    reasons: list[str] = []
    actions: list[str] = []

    # 1. Label eligibility
    excluded = excluded_issue_labels(issue, frozenset(config.github.exclude_labels))
    if excluded:
        reason_codes.append("EXCLUDED_LABEL")
        reasons.append(f"Issue has excluded label(s): {', '.join(sorted(excluded))}")
        actions.append("Remove the excluded label from the issue if it should be processed.")

    # 2. Dependency cycles and unfinished dependencies
    cycles = detect_dependency_cycles(tasks)
    if issue.number in cycles or (task is not None and task.status is TaskState.BLOCKED):
        reason_codes.append("DEPENDENCY_CYCLE")
        reasons.append("Issue is blocked by a dependency cycle or blocked prerequisite.")
        actions.append("Resolve the dependency cycle or unblock prerequisite issues.")

    deps = task.dependencies if task is not None else parse_dependencies(issue.body)
    if deps:
        unfinished: list[int] = []
        for dep in deps:
            dep_task = task_map.get(dep)
            if (
                dep_task is None
                or dep_task.status is not TaskState.COMPLETE
                or (dep_task.pr is not None and dep_task.completion_kind != "merged")
            ):
                unfinished.append(dep)
        if unfinished:
            reason_codes.append("UNFINISHED_DEPENDENCY")
            reasons.append(f"Issue has unfinished parent dependencies: {unfinished}")
            actions.append(f"Wait for parent issues {unfinished} to be completed and merged.")

    # 3. Existing task states
    if task is not None:
        if task.status is TaskState.NEEDS_HUMAN:
            reason_codes.append("NEEDS_HUMAN")
            reason_str = task.needs_human_reason or "unknown"
            reasons.append(f"Issue needs human intervention: {reason_str}")
            actions.append(
                "Diagnose the failure in the worktree, resolve it, and reset/resolve the task."
            )
        elif task.status is TaskState.COMPLETE:
            reason_codes.append("COMPLETE")
            reasons.append("Task is already complete.")
        elif task.status is TaskState.CANCELLED:
            reason_codes.append("CANCELLED")
            reasons.append("Task was cancelled.")
            actions.append("Use subsched reset to restore the task to READY if needed.")
        elif task.status is TaskState.IN_PROGRESS:
            reason_codes.append("IN_PROGRESS")
            reasons.append("Task is currently in progress.")
        elif task.status is TaskState.READY_FOR_REVIEW:
            reason_codes.append("READY_FOR_REVIEW")
            reasons.append("Task PR is waiting for review/CI/merge.")

    # 4. Run budget
    dispatched = run_dispatched_issues or set()
    budget = config.execution.max_tasks_per_run
    if len(dispatched) >= budget and issue.number not in dispatched:
        reason_codes.append("RUN_BUDGET_EXCEEDED")
        reasons.append(f"Run budget reached ({len(dispatched)}/{budget} tasks dispatched).")
        actions.append("Increase max_tasks_per_run or run a new scheduler cycle.")

    # 5. Capacity & Routing
    router = Router(
        AgentConfig(name, priority=settings.priority, enabled=settings.enabled)
        for name, settings in config.agents.items()
    )
    cap_list = list(capacities)
    selected_provider = router.select(cap_list, now=current)

    if selected_provider is None:
        has_stale = False
        has_cooldown = False
        has_available_enabled = False

        for cap in cap_list:
            if cap.agent in config.agents and config.agents[cap.agent].enabled:
                if cap.source == "provider" and not _is_fresh_provider(cap, current):
                    has_stale = True
                elif not cap.is_available(current):
                    has_cooldown = True
                elif cap.is_available(current):
                    has_available_enabled = True

        if has_stale:
            reason_codes.append("CAPACITY_STALE")
            reasons.append(
                "Provider capacity observation is stale (older than 5 minutes or in future)."
            )
            actions.append("Wait for fresh capacity probe or trigger a new probe.")
        elif has_cooldown:
            reason_codes.append("CAPACITY_COOLDOWN")
            reasons.append("Provider is currently in cooldown.")
            actions.append("Wait for cooldown to expire.")
        elif not cap_list or not has_available_enabled:
            if not any(
                code in reason_codes
                for code in ("EXCLUDED_LABEL", "DEPENDENCY_CYCLE", "UNFINISHED_DEPENDENCY")
            ):
                reason_codes.append("NO_CAPACITY")
                reasons.append("No enabled provider has available capacity.")
                actions.append("Check provider configuration and capacity.")

    dispatchable = selected_provider is not None and len(reason_codes) == 0

    return {
        "issue_number": issue.number,
        "title": issue.title,
        "dispatchable": dispatchable,
        "selected_provider": selected_provider if dispatchable else None,
        "reason_codes": reason_codes,
        "reasons": reasons,
        "operator_actions": actions,
        "current_status": task.status.value if task else "UNQUEUED",
    }
