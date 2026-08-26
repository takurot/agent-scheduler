import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from subsched.models import Capacity, CapacityState, Issue, Task, TaskState
from subsched.storage import (
    JsonStateStore,
    LockRecord,
    SchedulerLock,
    StateCorruptionError,
    find_repository_root,
    get_process_start_time,
)


def test_state_round_trip_preserves_recovery_fields(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    task = Task.from_issue(Issue(number=7, title="seven")).with_worktree("/worktrees/7")
    task = task.transition(TaskState.DISPATCHED, current_agent="claude")

    store.save_tasks((task,))

    assert store.load_tasks() == (task,)


def test_corrupt_state_quarantined_and_fails_closed(tmp_path: Path) -> None:
    state_file = tmp_path / ".ai" / "scheduler.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{broken", encoding="utf-8")

    with pytest.raises(StateCorruptionError):
        JsonStateStore(tmp_path).load_tasks()

    assert not state_file.exists()
    quarantine_files = list((tmp_path / ".ai" / "quarantine").glob("*.corrupt.json"))
    assert len(quarantine_files) == 1
    assert quarantine_files[0].read_text(encoding="utf-8") == "{broken"


def test_unknown_schema_version_quarantined_and_fails_closed(tmp_path: Path) -> None:
    state_file = tmp_path / ".ai" / "scheduler.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps({"schema_version": 999, "tasks": []}), encoding="utf-8")

    with pytest.raises(StateCorruptionError, match="schema"):
        JsonStateStore(tmp_path).load_tasks()

    assert not state_file.exists()
    quarantine_files = list((tmp_path / ".ai" / "quarantine").glob("*.corrupt.json"))
    assert len(quarantine_files) == 1


def test_duplicate_task_quarantined(tmp_path: Path) -> None:
    state_file = tmp_path / ".ai" / "scheduler.json"
    state_file.parent.mkdir(parents=True)
    t = Task.from_issue(Issue(number=1, title="one")).to_dict()
    state_file.write_text(
        json.dumps({"schema_version": 1, "tasks": [t, t]}), encoding="utf-8"
    )

    with pytest.raises(StateCorruptionError, match="duplicate task"):
        JsonStateStore(tmp_path).load_tasks()


def test_invalid_capacity_quarantined(tmp_path: Path) -> None:
    state_file = tmp_path / ".ai" / "scheduler.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps({"schema_version": 1, "tasks": [], "capacities": "invalid"}),
        encoding="utf-8",
    )

    with pytest.raises(StateCorruptionError, match="invalid capacity"):
        JsonStateStore(tmp_path).load_capacities()


def test_invalid_paused_flag_quarantined(tmp_path: Path) -> None:
    state_file = tmp_path / ".ai" / "scheduler.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps({"schema_version": 1, "tasks": [], "paused": "not-bool"}),
        encoding="utf-8",
    )

    with pytest.raises(StateCorruptionError, match="must be boolean"):
        JsonStateStore(tmp_path).is_paused()


def test_oversized_state_quarantined(tmp_path: Path) -> None:
    state_file = tmp_path / ".ai" / "scheduler.json"
    state_file.parent.mkdir(parents=True)
    # Write > 10MB
    state_file.write_bytes(b"x" * (11 * 1024 * 1024))

    with pytest.raises(StateCorruptionError, match="exceeds the size limit"):
        JsonStateStore(tmp_path).load_tasks()


def test_standard_directory_initialization_and_backup(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.save_tasks(())

    for subdir in ("tasks", "handoffs", "runtime", "quarantine", "backup"):
        assert (tmp_path / ".ai" / subdir).is_dir()

    # Second save creates backup
    task = Task.from_issue(Issue(number=1, title="one"))
    store.save_tasks((task,))
    assert (tmp_path / ".ai" / "backup" / "scheduler.bak.json").exists()


def test_capacity_cooldown_round_trip(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    capacity = Capacity(
        agent="claude",
        state=CapacityState.COOLDOWN_SESSION,
        reset_at=now + timedelta(hours=1),
        observed_at=now,
        source="structured_result",
        confidence="high",
    )
    store = JsonStateStore(tmp_path)

    store.save_state((), capacities=(capacity,))

    assert store.load_capacities() == (capacity,)


def test_state_store_paused_state(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    assert store.is_paused() is False

    store.set_paused(True)
    assert store.is_paused() is True

    store.set_paused(False)
    assert store.is_paused() is False


def test_state_store_rejects_symlinked_state_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / ".ai").symlink_to(outside, target_is_directory=True)

    with pytest.raises(StateCorruptionError, match="symlink"):
        JsonStateStore(tmp_path).save_tasks(())


def test_state_store_rejects_symlinked_repo(tmp_path: Path) -> None:
    real_repo = tmp_path / "real_repo"
    real_repo.mkdir()
    sym_repo = tmp_path / "sym_repo"
    sym_repo.symlink_to(real_repo, target_is_directory=True)

    with pytest.raises(StateCorruptionError, match="symlink"):
        JsonStateStore(sym_repo).save_tasks(())


def test_scheduler_rejects_persisted_available_capacity(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    capacity = Capacity(
        agent="codex",
        state=CapacityState.AVAILABLE,
        observed_at=now,
        source="provider",
        confidence="high",
    )
    store = JsonStateStore(tmp_path)
    store.save_state((), capacities=(capacity,))

    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker

    with pytest.raises(ValueError, match="cooldown blocker"):
        Scheduler(
            store=store,
            router=Router((AgentConfig("codex", 90),)),
            worker=ScriptedWorker({}),
            worktree_root=tmp_path / "worktrees",
        )


def test_scheduler_lock_mutual_exclusion(tmp_path: Path) -> None:
    lock_file = tmp_path / ".ai" / "scheduler.lock"
    lock1 = SchedulerLock(lock_file)
    lock2 = SchedulerLock(lock_file)

    lock1.acquire()
    assert lock1._held is True

    # Acquiring again on same instance is a no-op
    lock1.acquire()

    lock1.release()
    assert lock1._held is False

    # lock2 can now acquire
    lock2.acquire()
    assert lock2._held is True
    lock2.release()


def test_scheduler_lock_different_paths_do_not_block_across_threads(tmp_path: Path) -> None:
    """SchedulerLock instances guarding unrelated lock files (different
    repositories/state directories) must not contend for the same thread lock. Regression
    test for a class-level shared RLock that serialized unrelated stores."""
    lock_a = SchedulerLock(tmp_path / "repo-a" / ".ai" / "scheduler.lock")
    lock_b = SchedulerLock(tmp_path / "repo-b" / ".ai" / "scheduler.lock")

    lock_a.acquire()
    b_acquired = threading.Event()

    def acquire_b() -> None:
        lock_b.acquire()
        b_acquired.set()
        lock_b.release()

    thread = threading.Thread(target=acquire_b)
    try:
        thread.start()
        completed = b_acquired.wait(timeout=2.0)
        assert completed, "acquiring an unrelated lock_path blocked on another store's lock"
    finally:
        lock_a.release()
        thread.join(timeout=2.0)


def test_scheduler_lock_same_path_still_serializes_across_threads(tmp_path: Path) -> None:
    lock_file = tmp_path / ".ai" / "scheduler.lock"
    lock1 = SchedulerLock(lock_file)
    lock2 = SchedulerLock(lock_file)

    lock1.acquire()
    acquired = threading.Event()

    def acquire_second() -> None:
        lock2.acquire()
        acquired.set()
        lock2.release()

    thread = threading.Thread(target=acquire_second)
    try:
        thread.start()
        # lock2 must remain blocked while lock1 holds the same path's thread lock.
        assert not acquired.wait(timeout=0.3)
    finally:
        lock1.release()
        thread.join(timeout=2.0)
    assert acquired.is_set()


def test_scheduler_lock_stale_lock_recovery(tmp_path: Path) -> None:
    lock_file = tmp_path / ".ai" / "scheduler.lock"
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    # Stale record with non-existent dead PID 999999
    stale_record = LockRecord(
        pid=999999,
        process_start_time="2020-01-01T00:00:00Z",
        nonce="deadbeef",
        created_at="2020-01-01T00:00:00Z",
        hostname="old-host",
    )
    lock_file.write_text(json.dumps(stale_record.to_dict()), encoding="utf-8")

    lock = SchedulerLock(lock_file)
    lock.acquire()
    assert lock._held is True
    lock.release()


def test_process_start_time_helper() -> None:
    assert get_process_start_time(-1) is None
    st = get_process_start_time(os.getpid())
    assert st is not None or sys.platform != "darwin"


def test_state_store_cas_lost_update_detection(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.save_tasks(())
    rev = store.get_revision()

    # Save with matching expected_revision succeeds
    store.save_tasks((), expected_revision=rev)
    new_rev = store.get_revision()
    assert new_rev == rev + 1

    # Save with stale expected_revision raises StateCorruptionError
    with pytest.raises(StateCorruptionError, match="lost update"):
        store.save_tasks((), expected_revision=rev)


def test_invalid_revision_quarantined(tmp_path: Path) -> None:
    state_file = tmp_path / ".ai" / "scheduler.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(
        json.dumps({"schema_version": 1, "revision": "invalid_string", "tasks": []}),
        encoding="utf-8",
    )

    with pytest.raises(StateCorruptionError, match="revision"):
        JsonStateStore(tmp_path).load_tasks()

    assert not state_file.exists()
    quarantine_files = list((tmp_path / ".ai" / "quarantine").glob("*.corrupt.json"))
    assert len(quarantine_files) == 1


def test_lock_acquire_cleans_up_on_partial_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_file = tmp_path / ".ai" / "scheduler.lock"
    lock = SchedulerLock(lock_file)

    def mock_dump(*args: object, **kwargs: object) -> None:
        raise OSError("disk write failed")

    monkeypatch.setattr(json, "dump", mock_dump)

    with pytest.raises(OSError, match="disk write failed"):
        lock.acquire()

    assert not lock_file.exists()
    assert lock._held is False


def test_is_process_alive_permission_error_fallthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    from subsched.storage import is_process_alive

    def mock_kill(pid: int, sig: int) -> None:
        raise PermissionError("EPERM")

    monkeypatch.setattr(os, "kill", mock_kill)
    monkeypatch.setattr("subsched.storage.get_process_start_time", lambda pid: "start_time_abc")

    # Matching start time
    assert is_process_alive(1234, "start_time_abc") is True
    # Mismatched start time
    assert is_process_alive(1234, "different_start_time") is False
    # No expected start time
    assert is_process_alive(1234, None) is True


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-q", "-m", "init"], check=True
    )


def test_find_repository_root_resolves_subdirectory_to_toplevel(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    subdir = tmp_path / "src" / "nested"
    subdir.mkdir(parents=True)

    root = find_repository_root(subdir)

    assert root == tmp_path.resolve()


def test_find_repository_root_falls_back_when_not_a_git_repo(tmp_path: Path) -> None:
    non_repo = tmp_path / "plain"
    non_repo.mkdir()

    root = find_repository_root(non_repo)

    assert root == non_repo


def test_find_repository_root_falls_back_on_execution_failure(tmp_path: Path) -> None:
    def failing_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        raise OSError("git not found")

    root = find_repository_root(tmp_path, run=failing_run)

    assert root == tmp_path


def test_find_repository_root_falls_back_on_nonzero_exit(tmp_path: Path) -> None:
    def failing_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 128, stdout="", stderr="not a git repository")

    root = find_repository_root(tmp_path, run=failing_run)

    assert root == tmp_path

