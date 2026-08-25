from __future__ import annotations

from pathlib import Path

from subsched.models import Issue
from subsched.router import AgentConfig, Router
from subsched.scheduler import Scheduler, ScriptedWorker
from subsched.storage import JsonStateStore, SchedulerLockError


def _scheduler(tmp_path: Path) -> Scheduler:
    return Scheduler(
        store=JsonStateStore(tmp_path),
        router=Router((AgentConfig("claude", 100), AgentConfig("codex", 90))),
        worker=ScriptedWorker({}),
        worktree_root=tmp_path / "worktrees",
    )


def test_persist_is_blocked_while_another_process_holds_the_scheduler_lock(
    tmp_path: Path,
) -> None:
    """Scheduler._persist() must serialize with CLI writers (pause/resume/cancel) via the
    same process-level lock, so a concurrent CLI command cannot race a Scheduler tick and
    silently lose either write."""
    store = JsonStateStore(tmp_path)
    scheduler = _scheduler(tmp_path)

    external_lock = store.lock()
    external_lock.acquire()
    try:
        try:
            scheduler.discover((Issue(number=101, title="one"),))
        except SchedulerLockError:
            pass
        else:
            raise AssertionError(
                "expected SchedulerLockError while the lock is held by another process"
            )
    finally:
        external_lock.release()

    # The in-memory queue mutation from the failed discover() call must not have leaked
    # into persisted state.
    assert store.load_tasks() == ()


def test_persist_succeeds_and_releases_the_lock_for_subsequent_writers(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    scheduler = _scheduler(tmp_path)

    scheduler.discover((Issue(number=101, title="one"),))
    assert [task.issue_number for task in store.load_tasks()] == [101]

    # A subsequent external writer (e.g. `subsched cancel`) must be able to take the lock
    # immediately afterwards; no deadlock or leaked lock file.
    with store.lock():
        store.save_tasks(store.load_tasks(), paused=True)
    assert store.is_paused() is True
