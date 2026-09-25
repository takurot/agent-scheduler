from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from subsched.gitenv import ensure_git_exclude
from subsched.maintenance import (
    ArchiveDecision,
    apply_archive_plan,
    build_maintenance_report,
    directory_usage,
)
from subsched.models import Task, TaskState
from subsched.storage import JsonStateStore


def _task(
    issue: int,
    worktree: Path,
    *,
    status: TaskState = TaskState.COMPLETE,
    pr: int | None = None,
    completion_kind: str | None = None,
) -> Task:
    return Task(
        task_id=f"github-{issue}",
        issue_number=issue,
        title=f"Task {issue}",
        labels=(),
        status=status,
        worktree=str(worktree),
        dependencies=(99,),
        pr=pr,
        completion_kind=completion_kind,
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    (path / "tracked.txt").write_text("tracked\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "--quiet", "-m", "initial"], check=True)


def _add_worktree(repo: Path, issue: int) -> Path:
    worktree = repo / ".ai" / "worktrees" / f"issue-{issue}"
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "worktree",
            "add",
            "--quiet",
            "-b",
            f"subsched/issue-{issue}",
            str(worktree),
        ],
        check=True,
    )
    ensure_git_exclude(worktree)
    return worktree


def test_directory_usage_counts_files_without_following_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    (root / "one").write_bytes(b"1234")
    nested = root / "nested"
    nested.mkdir()
    (nested / "two").write_bytes(b"12")
    outside = tmp_path / "outside"
    outside.write_bytes(b"x" * 100)
    (root / "link").symlink_to(outside)

    usage = directory_usage(root)

    assert usage.bytes == 6
    assert usage.files == 2
    assert usage.symlinks == 1


def test_report_excludes_unmerged_active_dirty_untracked_and_symlinked_worktrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    tasks: list[Task] = []
    for issue in range(1, 8):
        path = store.worktrees_dir / f"issue-{issue}"
        path.mkdir()
        tasks.append(_task(issue, path, pr=issue * 10, completion_kind="merged"))
    tasks[1] = replace(tasks[1], status=TaskState.IN_PROGRESS, completion_kind=None)
    tasks[2] = replace(tasks[2], completion_kind=None)
    (Path(tasks[3].worktree or "") / ".ai" / "runtime").mkdir(parents=True)
    (Path(tasks[3].worktree or "") / ".ai" / "runtime" / "4.process.json").write_text(
        "{}", encoding="utf-8"
    )
    symlink_target = tmp_path / "symlink-target"
    symlink_target.mkdir()
    symlink_path = Path(tasks[5].worktree or "")
    symlink_path.rmdir()
    symlink_path.symlink_to(symlink_target)
    store.save_tasks(tasks)

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        worktree = Path(argv[2])
        if "symbolic-ref" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=f"refs/heads/subsched/{worktree.name}\n",
                stderr="",
            )
        if worktree.name == "issue-5":
            return subprocess.CompletedProcess(argv, 0, stdout="?? notes.txt\0", stderr="")
        if worktree.name == "issue-6":
            raise AssertionError("git must not inspect a symlinked worktree")
        if worktree.name == "issue-7":
            return subprocess.CompletedProcess(argv, 0, stdout=" M tracked.py\0", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    report = build_maintenance_report(tmp_path, store)
    decisions = {item.issue_number: item for item in report.worktrees}

    assert decisions[1].eligible is True
    assert decisions[2].eligible is False
    assert "task is active" in decisions[2].reasons
    assert "merged completion is not verified" in decisions[3].reasons
    assert "active process record exists" in decisions[4].reasons
    assert "untracked files" in decisions[5].reasons
    assert "symlink" in decisions[6].reasons
    assert "tracked changes" in decisions[7].reasons


def test_apply_revalidates_and_refuses_candidate_that_became_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    worktree = store.worktrees_dir / "issue-1"
    worktree.mkdir()
    store.save_tasks((_task(1, worktree),))

    monkeypatch.setattr(
        "subsched.maintenance.assess_archive_candidate",
        lambda *_args, **_kwargs: ArchiveDecision(1, str(worktree), True, ()),
    )
    report = build_maintenance_report(tmp_path, store)
    monkeypatch.setattr(
        "subsched.maintenance.assess_archive_candidate",
        lambda *_args, **_kwargs: ArchiveDecision(
            1, str(worktree), False, ("untracked files are present",)
        ),
    )

    results = apply_archive_plan(tmp_path, store, report)

    assert results[0].archived is False
    assert "untracked files" in results[0].reason
    assert worktree.exists()
    assert not (store.state_dir / "archive" / "issue-1").exists()


def test_apply_archives_clean_completed_worktree_and_preserves_task_history(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    store = JsonStateStore(repo)
    store.init_directories()
    worktree = _add_worktree(repo, 7)
    handoff = worktree / ".ai" / "handoffs" / "7.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("handoff evidence\n", encoding="utf-8")
    task = _task(7, worktree, pr=70, completion_kind="merged")
    store.save_tasks((task,))

    report = build_maintenance_report(repo, store)
    results = apply_archive_plan(repo, store, report)

    assert results[0].archived is True
    assert not worktree.exists()
    archive = store.state_dir / "archive" / "issue-7"
    manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["issue_number"] == 7
    assert manifest["completion_kind"] == "merged"
    assert manifest["dependencies"] == [99]
    assert (archive / "repository.bundle").is_file()
    assert (archive / "artifacts" / "handoffs" / "7.md").read_text(
        encoding="utf-8"
    ) == "handoff evidence\n"
    assert "git worktree add" in (archive / "RESTORE.md").read_text(encoding="utf-8")
    loaded = store.load_tasks()[0]
    assert loaded.status is TaskState.COMPLETE
    assert loaded.dependencies == (99,)
    assert loaded.pr == 70


def test_apply_rejects_symlinked_recovery_artifact_without_following_it(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    store = JsonStateStore(repo)
    store.init_directories()
    worktree = _add_worktree(repo, 8)
    handoffs = worktree / ".ai" / "handoffs"
    handoffs.mkdir(parents=True)
    outside = tmp_path / "outside-secret"
    outside.write_text("must not be copied\n", encoding="utf-8")
    (handoffs / "8.md").symlink_to(outside)
    store.save_tasks((_task(8, worktree, pr=80, completion_kind="merged"),))

    report = build_maintenance_report(repo, store)
    results = apply_archive_plan(repo, store, report)

    assert report.candidates == (8,)
    assert results[0].archived is False
    assert "symlink" in results[0].reason
    assert worktree.exists()
    assert not (store.state_dir / "archive" / "issue-8").exists()


def test_failed_git_removal_retains_both_archive_and_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    store = JsonStateStore(repo)
    store.init_directories()
    worktree = _add_worktree(repo, 9)
    store.save_tasks((_task(9, worktree, pr=90, completion_kind="merged"),))
    report = build_maintenance_report(repo, store)
    real_run = subprocess.run

    def change_before_remove(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if "worktree" in argv and "remove" in argv:
            (worktree / "tracked.txt").write_text("changed concurrently\n", encoding="utf-8")
        return real_run(argv, **kwargs)  # type: ignore[arg-type]

    results = apply_archive_plan(repo, store, report, run=change_before_remove)

    assert results[0].archived is False
    assert "git worktree remove failed" in results[0].reason
    assert worktree.exists()
    assert (store.state_dir / "archive" / "issue-9" / "manifest.json").is_file()
