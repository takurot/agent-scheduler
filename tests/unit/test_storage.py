import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from subsched.gitenv import GIT_LOCATION_OVERRIDE_VARS
from subsched.models import Capacity, CapacityState, Issue, Task, TaskState
from subsched.storage import (
    JsonStateStore,
    LockRecord,
    SchedulerLock,
    StateCorruptionError,
    atomic_write_secure_bytes,
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


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file permission bits are not meaningful on Windows"
)
def test_corrupt_state_quarantine_is_secured_without_prior_initialization(
    tmp_path: Path,
) -> None:
    import stat

    state_file = tmp_path / ".ai" / "scheduler.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text("{broken", encoding="utf-8")
    os.chmod(state_file, 0o644)

    with pytest.raises(StateCorruptionError):
        JsonStateStore(tmp_path).load_tasks()

    quarantine_dir = tmp_path / ".ai" / "quarantine"
    quarantined = next(quarantine_dir.glob("*.corrupt.json"))
    assert stat.S_IMODE(quarantine_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(quarantined.stat().st_mode) == 0o600


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

    for subdir in ("tasks", "handoffs", "runtime", "quarantine", "backup", "worktrees"):
        assert (tmp_path / ".ai" / subdir).is_dir()

    # Second save creates backup
    task = Task.from_issue(Issue(number=1, title="one"))
    store.save_tasks((task,))
    assert (tmp_path / ".ai" / "backup" / "scheduler.bak.json").exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file permission bits are not meaningful on Windows"
)
def test_state_store_repairs_runtime_directory_permissions(tmp_path: Path) -> None:
    import stat

    worktrees = tmp_path / ".ai" / "worktrees"
    worktrees.mkdir(parents=True, mode=0o755)
    os.chmod(worktrees, 0o755)

    JsonStateStore(tmp_path).init_directories()

    for subdir in ("tasks", "handoffs", "runtime", "quarantine", "backup", "worktrees"):
        assert stat.S_IMODE((tmp_path / ".ai" / subdir).stat().st_mode) == 0o700


@pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX file permission bits are not meaningful on Windows"
)
def test_backup_file_hardened_to_0600_under_permissive_umask(tmp_path: Path) -> None:
    """Regression test for #143: the scheduler.bak.json backup was written via a plain
    Path.write_bytes(), so under a permissive umask it ended up 0644 -- world-/group-
    readable runtime state -- unlike scheduler.json itself, which was already 0600 via
    tempfile.mkstemp."""
    import stat

    old_umask = os.umask(0o022)
    try:
        store = JsonStateStore(tmp_path)
        store.save_tasks(())
        store.save_tasks((Task.from_issue(Issue(number=1, title="one")),))
    finally:
        os.umask(old_umask)

    backup_file = tmp_path / ".ai" / "backup" / "scheduler.bak.json"
    assert backup_file.exists()
    assert stat.S_IMODE(backup_file.stat().st_mode) == 0o600


def test_preexisting_backup_permissions_are_repaired_without_deleting_content(
    tmp_path: Path,
) -> None:
    """A backup file left behind (0644) by a pre-#143 subsched version must be repaired
    to 0600 the next time the state directories are initialized, without deleting it."""
    import stat

    backup_dir = tmp_path / ".ai" / "backup"
    backup_dir.mkdir(parents=True)
    stale = backup_dir / "scheduler.bak.json"
    stale.write_text('{"stale": true}', encoding="utf-8")
    os.chmod(stale, 0o644)

    store = JsonStateStore(tmp_path)
    store.save_tasks(())

    assert stale.exists()
    assert stale.read_text(encoding="utf-8") == '{"stale": true}'
    assert stat.S_IMODE(stale.stat().st_mode) == 0o600


def test_atomic_write_secure_bytes_rejects_symlinked_target_file(tmp_path: Path) -> None:
    """Regression test for #143 code review: a pre-existing symlink AT the destination
    path (directory itself untouched/real) must be rejected fail-closed -- covering the
    TOCTOU/symlink-swap scenario the hardening exists for -- and the symlink's outside
    target must be left untouched, not silently written through."""
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_target = outside_dir / "secret.txt"
    outside_target.write_text("do not overwrite me", encoding="utf-8")

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    victim = real_dir / "victim.json"
    victim.symlink_to(outside_target)

    with pytest.raises(OSError, match="symlink"):
        atomic_write_secure_bytes(victim, b'{"attacker": "controlled"}')

    assert outside_target.read_text(encoding="utf-8") == "do not overwrite me"


def test_atomic_write_secure_bytes_rejects_symlinked_parent_directory(tmp_path: Path) -> None:
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    sym_dir = tmp_path / "sym"
    sym_dir.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(OSError, match="symlink"):
        atomic_write_secure_bytes(sym_dir / "victim.json", b"{}")

    assert list(outside_dir.iterdir()) == []


def test_backup_write_is_atomic_no_leftover_tmp_file(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.save_tasks(())
    store.save_tasks((Task.from_issue(Issue(number=1, title="one")),))

    backup_dir = tmp_path / ".ai" / "backup"
    assert list(backup_dir.glob("*.tmp")) == []
    backup_file = backup_dir / "scheduler.bak.json"
    assert json.loads(backup_file.read_text(encoding="utf-8"))["schema_version"] == 1


def test_gitignore_covers_runtime_state(tmp_path: Path) -> None:
    """Regression test for #143: .ai/backup/ and .ai/checkpoints/ must be untracked so
    runtime state (which can contain agent output, test results, and changed-file
    lists) never shows up in `git status` or gets accidentally committed."""
    repo_root = Path(__file__).resolve().parents[2]
    gitignore = (repo_root / ".gitignore").read_text(encoding="utf-8")
    assert ".ai/backup/" in gitignore
    assert ".ai/checkpoints/" in gitignore
    assert ".ai/quarantine/" in gitignore

    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    (tmp_path / ".gitignore").write_text(gitignore, encoding="utf-8")
    backup_dir = tmp_path / ".ai" / "backup"
    backup_dir.mkdir(parents=True)
    (backup_dir / "scheduler.bak.json").write_text("{}", encoding="utf-8")
    checkpoints_dir = tmp_path / ".ai" / "checkpoints"
    checkpoints_dir.mkdir(parents=True)
    (checkpoints_dir / "130.json").write_text("{}", encoding="utf-8")
    quarantine_dir = tmp_path / ".ai" / "quarantine"
    quarantine_dir.mkdir(parents=True)
    (quarantine_dir / "scheduler.corrupt.json").write_text("{}", encoding="utf-8")

    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=str(tmp_path),
        check=True,
        capture_output=True,
        text=True,
    )
    assert "backup" not in result.stdout
    assert "checkpoints" not in result.stdout
    assert "quarantine" not in result.stdout


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


def test_find_repository_root_strips_git_location_override_env_vars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaked GIT_DIR/GIT_WORK_TREE must never redirect the probe away from `start`
    (issue #147)."""
    monkeypatch.setenv("GIT_DIR", "/leaked/.git")
    captured: dict[str, object] = {}

    def spying_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=str(tmp_path), stderr="")

    find_repository_root(tmp_path, run=spying_run)

    env = captured.get("env")
    assert isinstance(env, dict)
    for name in GIT_LOCATION_OVERRIDE_VARS:
        assert name not in env
