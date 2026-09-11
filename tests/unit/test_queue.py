from subsched.models import Issue, Task, TaskState
from subsched.queue import TaskQueue


def task(number: int, *labels: str) -> Task:
    return Task.from_issue(Issue(number=number, title=str(number), labels=labels))


def test_queue_orders_by_priority_then_issue_number() -> None:
    queue = TaskQueue(
        tasks=(task(9, "enhancement"), task(4, "bug"), task(2, "bug")),
        label_scores={"bug": 60, "enhancement": 40},
    )

    assert [item.issue_number for item in queue.ready()] == [2, 4, 9]


def test_requeue_front_preserves_worktree_and_progress() -> None:
    original = task(4).with_worktree("/tmp/issue-4")
    queue = TaskQueue(tasks=(original, task(5)))
    running = original.transition(TaskState.DISPATCHED).transition(TaskState.IN_PROGRESS)
    queue = queue.replace(running)

    updated = queue.requeue_after_capacity_event(4)

    first = updated.ready()[0]
    assert first.issue_number == 4
    assert first.worktree == "/tmp/issue-4"
    assert first.attempt == 0


def _to_pr_ready(number: int) -> Task:
    return (
        task(number)
        .transition(TaskState.DISPATCHED)
        .transition(TaskState.IN_PROGRESS)
        .transition(TaskState.VERIFYING)
        .transition(TaskState.PR_READY)
    )


def test_queue_ready_includes_pr_review_and_revising() -> None:
    """#281: PR_REVIEW/REVISING are dispatchable states too -- see Task.dispatch_status."""
    normal = task(1)
    reviewing = _to_pr_ready(2).transition(TaskState.PR_REVIEW)
    revising = _to_pr_ready(3).transition(TaskState.PR_REVIEW).transition(TaskState.REVISING)
    queue = TaskQueue(tasks=(normal, reviewing, revising))

    assert [item.issue_number for item in queue.ready()] == [1, 2, 3]


def test_queue_rejects_duplicate_issue() -> None:
    first = task(1)

    try:
        TaskQueue(tasks=(first, first))
    except ValueError as error:
        assert "duplicate" in str(error)
    else:
        raise AssertionError("duplicate task was accepted")
