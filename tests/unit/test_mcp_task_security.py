"""Read-only MCP task boundaries (issue #451); credentials are synthetic."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from subsched.mcp_server import McpToolError, get_task_handoff_resource, inspect_task
from subsched.models import Task, TaskState
from subsched.storage import JsonStateStore

SECRET = "github_pat_synthetic_issue451"
HANDOFF = "# Issue\n\n#1 Synthetic task\n\n" + "\n\n".join(
    f"## {header}\n\n{SECRET if header != 'Timestamp' else '2026-10-08T00:00:00Z'}"
    for header in (
        "Goal", "Current Plan", "Completed", "Current Work", "Decisions",
        "Known Broken State", "Next Action", "Timestamp",
    )
)


def save_task(repo: Path, worktree: Path) -> JsonStateStore:
    store = JsonStateStore(repo)
    store.init_directories()
    store.save_tasks((Task(
        task_id="github-1", issue_number=1, title=SECRET, description=SECRET,
        labels=(SECRET,), status=TaskState.NEEDS_HUMAN,
        worktree=str(worktree), dispatch_model=SECRET,
    ),))
    return store


def write_handoff(worktree: Path) -> Path:
    path = worktree / ".ai" / "handoffs" / "1.md"
    path.parent.mkdir(parents=True)
    path.write_text(HANDOFF)
    return path


@pytest.mark.parametrize("reader", [inspect_task, get_task_handoff_resource])
@pytest.mark.parametrize("component", ["worktrees", "worktree", "ai", "handoffs", "file"])
def test_rejects_symlink_components_without_log_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    reader: Callable[[int, str], Any], component: str,
) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".ai" / "worktrees" / "issue-1"
    store = save_task(repo, worktree)
    handoff = write_handoff(worktree)
    targets = {
        "worktrees": worktree.parent, "worktree": worktree,
        "ai": worktree / ".ai", "handoffs": handoff.parent, "file": handoff,
    }
    target = targets[component]
    moved = tmp_path / "outside"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=component != "file")
    before = store.path.read_bytes()
    real_run = subprocess.run
    logs = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "log" in argv:
            logs.append(argv)
        return real_run(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("subsched.mcp_server.subprocess.run", run)
    with pytest.raises(McpToolError):
        reader(1, str(repo))
    assert not logs
    assert store.path.read_bytes() == before
    assert handoff.read_text() == HANDOFF


@pytest.mark.parametrize("reader", [inspect_task, get_task_handoff_resource])
@pytest.mark.parametrize("location", ["external", "sibling", "relative", "traversal"])
def test_rejects_unmanaged_worktree(
    tmp_path: Path, reader: Callable[[int, str], Any], location: str,
) -> None:
    repo = tmp_path / "repo"
    paths = {
        "external": tmp_path / "outside", "sibling": repo / "wt",
        "relative": Path(".ai/worktrees/issue-1"),
        "traversal": repo / ".ai" / "worktrees" / ".." / "outside",
    }
    worktree = paths[location]
    store = save_task(repo, worktree)
    if location != "relative":
        write_handoff(worktree)
    before = store.path.read_bytes()
    with pytest.raises(McpToolError):
        reader(1, str(repo))
    assert store.path.read_bytes() == before


@pytest.mark.parametrize("reader", [inspect_task, get_task_handoff_resource])
def test_rejects_repository_parent_symlink(
    tmp_path: Path, reader: Callable[[int, str], Any],
) -> None:
    repo = tmp_path / "real" / "repo"
    worktree = repo / ".ai" / "worktrees" / "issue-1"
    save_task(repo, worktree)
    write_handoff(worktree)
    (tmp_path / "link").symlink_to(repo.parent, target_is_directory=True)
    with pytest.raises(McpToolError):
        reader(1, str(tmp_path / "link" / "repo"))


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
    ).stdout


@pytest.mark.parametrize("redirect", ["none", "file", "symlink"])
@pytest.mark.parametrize("registered", [False, True])
def test_commits_use_trusted_repository_and_redact(
    tmp_path: Path, redirect: str, registered: bool,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
        "commit", "-q", "--allow-empty", "-m", f"trusted {SECRET}")
    worktree = repo / ".ai" / "worktrees" / "issue-1"
    save_task(repo, worktree)
    if registered:
        git(repo, "worktree", "add", "-b", "subsched/issue-1", str(worktree))
    else:
        git(repo, "branch", "subsched/issue-1")
    handoff = write_handoff(worktree)
    if redirect != "none":
        external = tmp_path / "external"
        external.mkdir()
        git(external, "init", "-q", "-b", "main")
        git(external, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "commit", "-q", "--allow-empty", "-m", "EXTERNAL_COMMIT")
        metadata = worktree / ".git"
        if metadata.exists():
            metadata.unlink()  # synthetic fixture's linked-worktree gitfile only
        if redirect == "file":
            metadata.write_text(f"gitdir: {external / '.git'}\n")
        else:
            metadata.symlink_to(external / ".git", target_is_directory=True)
    result = inspect_task(1, str(repo))
    assert len(result["recent_commits"]) == 1
    assert "trusted [REDACTED]" in result["recent_commits"][0]
    assert "EXTERNAL_COMMIT" not in json.dumps(result)
    assert SECRET not in json.dumps(result)
    assert result["handoff"]["goal"] == "[REDACTED]"
    assert SECRET not in get_task_handoff_resource(1, str(repo))
    assert handoff.read_text() == HANDOFF


@pytest.mark.parametrize("reader", [inspect_task, get_task_handoff_resource])
@pytest.mark.parametrize("component", ["repository", "state"])
def test_rejects_repository_or_state_symlink(
    tmp_path: Path, reader: Callable[[int, str], Any], component: str,
) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".ai" / "worktrees" / "issue-1"
    store = save_task(repo, worktree)
    handoff = write_handoff(worktree)
    before = store.path.read_bytes()
    target = repo if component == "repository" else store.state_dir
    moved = tmp_path / "moved"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=True)
    with pytest.raises(McpToolError):
        reader(1, str(repo))
    assert store.path.read_bytes() == before
    assert handoff.read_text() == HANDOFF


@pytest.mark.parametrize("missing", ["ai", "handoffs", "file"])
def test_missing_handoff_preserves_inspection(tmp_path: Path, missing: str) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".ai" / "worktrees" / "issue-1"
    save_task(repo, worktree)
    worktree.mkdir()
    if missing != "ai":
        (worktree / ".ai").mkdir()
    if missing == "file":
        (worktree / ".ai" / "handoffs").mkdir()
    result = inspect_task(1, str(repo))
    assert result["handoff"] is None
    assert result["recent_commits"] == ()
    with pytest.raises(McpToolError, match="no handoff available"):
        get_task_handoff_resource(1, str(repo))


@pytest.mark.parametrize("reader", [inspect_task, get_task_handoff_resource])
@pytest.mark.parametrize("kind", ["directory", "fifo", "invalid_utf8"])
def test_rejects_nonregular_or_unreadable_handoff(
    tmp_path: Path, reader: Callable[[int, str], Any], kind: str,
) -> None:
    import os

    repo = tmp_path / "repo"
    worktree = repo / ".ai" / "worktrees" / "issue-1"
    save_task(repo, worktree)
    path = worktree / ".ai" / "handoffs" / "1.md"
    path.parent.mkdir(parents=True)
    if kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"\xff")
    with pytest.raises(McpToolError, match="unsafe task handoff"):
        reader(1, str(repo))


def test_handoff_directory_swap_cannot_redirect_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    repo = tmp_path / "repo"
    worktree = repo / ".ai" / "worktrees" / "issue-1"
    save_task(repo, worktree)
    handoff = write_handoff(worktree)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "1.md").write_text("EXTERNAL_HANDOFF")
    real_open = os.open
    swapped = False

    def open_file(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if path == "1.md" and not swapped:
            swapped = True
            handoff.parent.rename(worktree / ".ai" / "retained")
            handoff.parent.symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("subsched.mcp_server.os.open", open_file)
    content = get_task_handoff_resource(1, str(repo))
    assert swapped
    assert "EXTERNAL_HANDOFF" not in content
    assert "[REDACTED]" in content


@pytest.mark.parametrize("reader", [inspect_task, get_task_handoff_resource])
@pytest.mark.parametrize("value", ["", 0, 1, False, True, [], {"path": "outside"}])
def test_rejects_malformed_persisted_worktree(
    tmp_path: Path, reader: Callable[[int, str], Any], value: object,
) -> None:
    repo = tmp_path / "repo"
    worktree = repo / ".ai" / "worktrees" / "issue-1"
    store = save_task(repo, worktree)
    payload = json.loads(store.path.read_text())
    payload["tasks"][0]["worktree"] = value
    store.path.write_text(json.dumps(payload))
    before = store.path.read_bytes()
    with pytest.raises(McpToolError):
        reader(1, str(repo))
    assert store.path.read_bytes() == before
