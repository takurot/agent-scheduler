from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from subsched.agents.process import redact_sensitive_command_audit
from subsched.gitenv import git_safe_env
from subsched.models import Task, TaskState
from subsched.storage import JsonStateStore, atomic_write_secure_bytes, secure_directory

RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class MaintenanceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Usage:
    bytes: int = 0
    files: int = 0
    symlinks: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            bytes=self.bytes + other.bytes,
            files=self.files + other.files,
            symlinks=self.symlinks + other.symlinks,
        )

    def to_dict(self) -> dict[str, int]:
        return {"bytes": self.bytes, "files": self.files, "symlinks": self.symlinks}


@dataclass(frozen=True, slots=True)
class ArchiveDecision:
    issue_number: int
    worktree: str
    eligible: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_number": self.issue_number,
            "worktree": self.worktree,
            "eligible": self.eligible,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class MaintenanceReport:
    revision: int
    usage: dict[str, Usage]
    worktrees: tuple[ArchiveDecision, ...]

    @property
    def candidates(self) -> tuple[int, ...]:
        return tuple(item.issue_number for item in self.worktrees if item.eligible)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "usage": {name: value.to_dict() for name, value in self.usage.items()},
            "worktrees": [item.to_dict() for item in self.worktrees],
            "archive_candidates": list(self.candidates),
        }


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    issue_number: int
    archived: bool
    reason: str = ""
    archive_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_number": self.issue_number,
            "archived": self.archived,
            "reason": self.reason,
            "archive_path": self.archive_path,
        }


def _redact(text: str) -> str:
    return "\n".join(redact_sensitive_command_audit(tuple(text.splitlines())))


def directory_usage(path: Path) -> Usage:
    """Measure regular-file bytes without following symlinks."""
    if not path.exists() and not path.is_symlink():
        return Usage()
    try:
        mode = path.lstat().st_mode
    except OSError as error:
        raise MaintenanceError(f"cannot inspect {path}: {error}") from error
    if stat.S_ISLNK(mode):
        return Usage(symlinks=1)
    if stat.S_ISREG(mode):
        return Usage(bytes=path.lstat().st_size, files=1)
    if not stat.S_ISDIR(mode):
        return Usage()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise MaintenanceError(f"cannot open directory without following links: {path}") from error
    try:
        return _directory_usage_fd(descriptor, path)
    finally:
        os.close(descriptor)


def _directory_usage_fd(descriptor: int, display_path: Path) -> Usage:
    total = Usage()
    try:
        entries = tuple(os.scandir(descriptor))
    except OSError as error:
        raise MaintenanceError(f"cannot list {display_path}: {error}") from error
    for entry in entries:
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError as error:
            raise MaintenanceError(
                f"cannot inspect {display_path / entry.name}: {error}"
            ) from error
        if stat.S_ISLNK(info.st_mode):
            total += Usage(symlinks=1)
        elif stat.S_ISREG(info.st_mode):
            total += Usage(bytes=info.st_size, files=1)
        elif stat.S_ISDIR(info.st_mode):
            try:
                child = os.open(
                    entry.name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=descriptor,
                )
            except OSError as error:
                raise MaintenanceError(
                    f"cannot open directory without following links: {display_path / entry.name}"
                ) from error
            try:
                total += _directory_usage_fd(child, display_path / entry.name)
            finally:
                os.close(child)
    return total


def _usage_for(paths: Iterable[Path]) -> Usage:
    total = Usage()
    for path in paths:
        total += directory_usage(path)
    return total


_ACTIVE_STATES = frozenset(
    {
        TaskState.DISPATCHED,
        TaskState.PLANNING,
        TaskState.PLAN_REVIEW,
        TaskState.IN_PROGRESS,
        TaskState.VERIFYING,
        TaskState.PR_REVIEW,
        TaskState.REVISING,
    }
)


def _git_status_reasons(
    worktree: Path, *, run: RunCommand, timeout_seconds: float
) -> tuple[str, ...]:
    try:
        result = run(
            ["git", "-C", str(worktree), "status", "--porcelain=v1", "-z", "-uall"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            env=git_safe_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return (_redact(f"git status could not be verified: {error}"),)
    if result.returncode != 0:
        return (_redact(f"git status failed: {result.stderr.strip()}"),)

    tracked = False
    untracked = False
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        if entry.startswith("?? "):
            untracked = True
        else:
            tracked = True
    reasons: list[str] = []
    if tracked:
        reasons.append("tracked changes")
    if untracked:
        reasons.append("untracked files")
    return tuple(reasons)


def assess_archive_candidate(
    task: Task,
    store: JsonStateStore,
    *,
    run: RunCommand | None = None,
    timeout_seconds: float = 30.0,
) -> ArchiveDecision:
    runner = run or subprocess.run
    reasons: list[str] = []
    if type(task.issue_number) is not int or not 0 < task.issue_number <= 2**31 - 1:
        return ArchiveDecision(0, "", False, ("invalid issue number",))
    expected = store.worktrees_dir / f"issue-{task.issue_number}"
    recorded = (
        Path(task.worktree)
        if isinstance(task.worktree, str) and bool(task.worktree)
        else None
    )

    if task.status in _ACTIVE_STATES:
        reasons.append("task is active")
    elif task.status is not TaskState.COMPLETE:
        reasons.append("task is not COMPLETE")
    if task.pr is not None and task.completion_kind != "merged":
        reasons.append("merged completion is not verified")
    if recorded is None:
        reasons.append("task has no recorded worktree")
        return ArchiveDecision(task.issue_number, str(expected), False, tuple(reasons))
    if store.worktrees_dir.is_symlink() or recorded.is_symlink():
        reasons.append("symlink")
        return ArchiveDecision(task.issue_number, str(recorded), False, tuple(reasons))
    if recorded.absolute() != expected.absolute():
        reasons.append("recorded worktree does not match the expected task path")
        return ArchiveDecision(task.issue_number, str(recorded), False, tuple(reasons))
    if not recorded.is_dir():
        reasons.append("worktree is missing or is not a directory")
        return ArchiveDecision(task.issue_number, str(recorded), False, tuple(reasons))

    process_record = recorded / ".ai" / "runtime" / f"{task.issue_number}.process.json"
    if process_record.is_symlink() or process_record.exists():
        reasons.append("active process record exists")

    expected_branch = f"refs/heads/subsched/issue-{task.issue_number}"
    try:
        branch = runner(
            ["git", "-C", str(recorded), "symbolic-ref", "--quiet", "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            env=git_safe_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        reasons.append(_redact(f"worktree branch could not be verified: {error}"))
    else:
        if branch.returncode != 0 or branch.stdout.strip() != expected_branch:
            reasons.append("worktree branch does not match the expected task branch")

    reasons.extend(_git_status_reasons(recorded, run=runner, timeout_seconds=timeout_seconds))
    archive_path = store.state_dir / "archive" / f"issue-{task.issue_number}"
    if archive_path.is_symlink() or archive_path.exists():
        reasons.append("archive destination already exists")
    return ArchiveDecision(task.issue_number, str(recorded), not reasons, tuple(reasons))


def build_maintenance_report(
    repository: Path,
    store: JsonStateStore,
    *,
    run: RunCommand | None = None,
) -> MaintenanceReport:
    del repository  # The validated store root owns every path inspected here.
    runner = run or subprocess.run
    snapshot = store.load_snapshot()
    usage = {
        "state": _usage_for(
            (
                store.path,
                store.backup_dir,
                store.tasks_dir,
                store.handoffs_dir,
                store.state_dir / "checkpoints",
            )
        ),
        "logs": directory_usage(store.runtime_dir),
        "quarantine": directory_usage(store.quarantine_dir),
        "worktrees": directory_usage(store.worktrees_dir),
        "archives": directory_usage(store.state_dir / "archive"),
    }
    decisions = [assess_archive_candidate(task, store, run=runner) for task in snapshot.tasks]
    known = {task.issue_number for task in snapshot.tasks}
    if store.worktrees_dir.exists() and not store.worktrees_dir.is_symlink():
        for path in sorted(store.worktrees_dir.iterdir(), key=lambda item: item.name):
            if not path.name.startswith("issue-"):
                continue
            raw_issue = path.name.removeprefix("issue-")
            if not raw_issue.isdigit() or int(raw_issue) <= 0 or int(raw_issue) in known:
                continue
            decisions.append(
                ArchiveDecision(
                    issue_number=int(raw_issue),
                    worktree=str(path),
                    eligible=False,
                    reasons=("no matching task record exists",),
                )
            )
    decisions.sort(key=lambda item: item.issue_number)
    return MaintenanceReport(snapshot.revision, usage, tuple(decisions))


_ARTIFACT_DIRECTORIES = ("tasks", "handoffs", "checkpoints", "runtime", "plans", "reviews")


def _copy_tree_without_links(source: Path, destination: Path) -> None:
    """Copy a Scheduler artifact tree through dirfds, rejecting every link/special file."""
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    destination.mkdir(mode=0o700)
    destination_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _copy_tree_entries(source_fd, destination_fd, source)
    finally:
        os.close(destination_fd)
        os.close(source_fd)


def _copy_tree_entries(source_fd: int, destination_fd: int, display_path: Path) -> None:
    for entry in os.scandir(source_fd):
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            raise MaintenanceError(f"scheduler artifact path contains a symlink: {display_path}")
        if stat.S_ISDIR(info.st_mode):
            os.mkdir(entry.name, mode=0o700, dir_fd=destination_fd)
            child_source = os.open(
                entry.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=source_fd,
            )
            child_destination = os.open(
                entry.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=destination_fd,
            )
            try:
                _copy_tree_entries(
                    child_source, child_destination, display_path / entry.name
                )
            finally:
                os.close(child_destination)
                os.close(child_source)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise MaintenanceError(
                f"scheduler artifact path contains a special file: {display_path / entry.name}"
            )
        source_file = os.open(
            entry.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=source_fd
        )
        destination_file = os.open(
            entry.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=destination_fd,
        )
        source_handle = os.fdopen(source_file, "rb")
        try:
            destination_handle = os.fdopen(destination_file, "wb")
        except BaseException:
            source_handle.close()
            os.close(destination_file)
            raise
        with source_handle, destination_handle:
            try:
                shutil.copyfileobj(source_handle, destination_handle)
            except OSError as error:
                raise MaintenanceError(
                    f"cannot copy scheduler artifact: {display_path / entry.name}"
                ) from error


def _secure_tree(path: Path) -> None:
    for child in path.rglob("*"):
        if child.is_symlink():
            raise MaintenanceError(f"archive contains unexpected symlink: {child}")
        os.chmod(child, 0o700 if child.is_dir() else 0o600)


def _restore_text(issue_number: int) -> str:
    branch = f"subsched/issue-{issue_number}"
    return (
        f"# Restore issue #{issue_number}\n\n"
        "Run these commands from the repository root after reviewing the manifest.\n"
        "The task record remains in `.ai/scheduler.json`; do not rediscover or recreate it.\n\n"
        "```bash\n"
        f"git worktree add .ai/worktrees/issue-{issue_number} {branch}\n"
        f"cp -R .ai/archive/issue-{issue_number}/artifacts/. "
        f".ai/worktrees/issue-{issue_number}/.ai/\n"
        "```\n\n"
        "If the task branch was removed, inspect `repository.bundle` and restore it to a "
        "new operator-chosen branch before adding the worktree. Never overwrite an existing "
        "branch or worktree.\n"
    )


def _write_archive(
    repository: Path,
    store: JsonStateStore,
    task: Task,
    *,
    revision: int,
    run: RunCommand,
    timeout_seconds: float = 60.0,
) -> ArchiveResult:
    assert task.worktree is not None
    worktree = Path(task.worktree)
    archive_root = store.state_dir / "archive"
    if archive_root.is_symlink():
        return ArchiveResult(task.issue_number, False, "archive root is a symlink")
    secure_directory(archive_root)
    destination = archive_root / f"issue-{task.issue_number}"
    if destination.is_symlink() or destination.exists():
        return ArchiveResult(task.issue_number, False, "archive destination already exists")
    staging = archive_root / f".issue-{task.issue_number}.{secrets.token_hex(8)}.tmp"
    staging.mkdir(mode=0o700)
    try:
        bundle = staging / "repository.bundle"
        bundle_result = run(
            ["git", "-C", str(worktree), "bundle", "create", str(bundle), "HEAD"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            env=git_safe_env(),
            check=False,
        )
        if bundle_result.returncode != 0 or not bundle.is_file():
            raise MaintenanceError(
                _redact(f"git bundle creation failed: {bundle_result.stderr.strip()}")
            )

        artifact_root = staging / "artifacts"
        artifact_root.mkdir(mode=0o700)
        worktree_state = worktree / ".ai"
        for name in _ARTIFACT_DIRECTORIES:
            source = worktree_state / name
            if not source.exists():
                continue
            _copy_tree_without_links(source, artifact_root / name)

        manifest = {
            "schema_version": 1,
            "issue_number": task.issue_number,
            "task_id": task.task_id,
            "status": task.status.value,
            "pr": task.pr,
            "completion_kind": task.completion_kind,
            "dependencies": list(task.dependencies),
            "branch": f"subsched/issue-{task.issue_number}",
            "original_worktree": f".ai/worktrees/issue-{task.issue_number}",
            "scheduler_revision": revision,
            "archived_at": datetime.now(UTC).isoformat(),
        }
        atomic_write_secure_bytes(
            staging / "manifest.json",
            (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )
        atomic_write_secure_bytes(
            staging / "RESTORE.md", _restore_text(task.issue_number).encode("utf-8")
        )
        _secure_tree(staging)
        os.replace(staging, destination)

        removal = run(
            ["git", "-C", str(repository), "worktree", "remove", str(worktree)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            env=git_safe_env(),
            check=False,
        )
        if removal.returncode != 0:
            return ArchiveResult(
                task.issue_number,
                False,
                _redact(f"git worktree remove failed: {removal.stderr.strip()}"),
                str(destination),
            )
        return ArchiveResult(task.issue_number, True, archive_path=str(destination))
    except (MaintenanceError, OSError, subprocess.TimeoutExpired) as error:
        if staging.exists() and not staging.is_symlink():
            shutil.rmtree(staging)
        return ArchiveResult(task.issue_number, False, _redact(str(error)))


def apply_archive_plan(
    repository: Path,
    store: JsonStateStore,
    report: MaintenanceReport,
    *,
    run: RunCommand | None = None,
) -> tuple[ArchiveResult, ...]:
    """Apply only previewed candidates, reloading state and rechecking every boundary."""
    runner = run or subprocess.run
    results: list[ArchiveResult] = []
    with store.lock():
        snapshot = store.load_snapshot()
        tasks = {task.issue_number: task for task in snapshot.tasks}
        for issue_number in report.candidates:
            task = tasks.get(issue_number)
            if task is None:
                results.append(ArchiveResult(issue_number, False, "task no longer exists"))
                continue
            decision = assess_archive_candidate(task, store, run=runner)
            if not decision.eligible:
                results.append(ArchiveResult(issue_number, False, "; ".join(decision.reasons)))
                continue
            results.append(
                _write_archive(
                    repository,
                    store,
                    task,
                    revision=snapshot.revision,
                    run=runner,
                )
            )
    return tuple(results)
