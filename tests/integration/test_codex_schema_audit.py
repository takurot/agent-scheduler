from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from subsched.agents.codex import CodexApprovalMode
from subsched.agents.isolation import prepare_verification_git
from subsched.agents.native import NativeWorker
from subsched.agents.process import run_process_group
from subsched.audit_prepare import _checked_lock
from subsched.contract import bootstrap_task_files
from subsched.models import AgentResult, AgentResultKind, Issue, Task, TaskState


@pytest.mark.parametrize("linked", [False, True])
def test_native_schemas_preserve_clean_audit_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: bool
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str, cwd: Path = repo) -> str:
        return subprocess.check_output(["git", "-C", str(cwd), *args], text=True).strip()

    git("init", "-q")
    (repo / ".gitignore").write_bytes((Path(__file__).parents[2] / ".gitignore").read_bytes())
    (repo / "uv.lock").write_text("locked\n")
    (repo / "pyproject.toml").write_text("project\n")
    (repo / "candidate.py").write_text("candidate = 1\n")
    (repo / "scripts").mkdir()
    (repo / "scripts/offline-build-requirements.txt").write_text("build\n")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    # Reproduce the host-only exclusion that hid the old default schema location.
    (repo / ".git/info/exclude").write_text(".ai/\n")
    git("config", "credential.helper", "synthetic-host-only")
    worktree = repo
    if linked:
        worktree = tmp_path / "linked"
        git("worktree", "add", "-qb", "task", str(worktree))

    monkeypatch.setattr("subsched.agents.native.native_isolation_failure", lambda: None)
    adapter = MagicMock()
    adapter.execute.return_value = AgentResult(AgentResultKind.PASS)
    worker = NativeWorker(
        codex_agent=adapter,
        subscription_billing_verified=True,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
    task = Task.from_issue(Issue(550, "Schema audit regression"), worktree=str(worktree))
    bootstrap_task_files(worktree, task)
    for status in (TaskState.READY, TaskState.PLAN_REVIEW):
        assert worker.run(replace(task, status=status), "codex").kind is AgentResultKind.PASS
    assert git("status", "--porcelain=v1", "--untracked-files=all", cwd=worktree) == ""
    context = prepare_verification_git(worktree, tmp_path / "snapshot")
    assert "synthetic-host-only" not in (context.git_dir / "config").read_text()
    assert not (context.git_dir / "info/exclude").exists()
    assert _checked_lock(worktree, context, tmp_path, run_process_group) == b"locked\n"
    for call in adapter.execute.call_args_list:
        argv = call.args[0].argv
        assert Path(argv[argv.index("--output-schema") + 1]).parent == worktree / ".ai/runtime"

    # Neither host exclusions nor legitimate runtime files may hide candidate dirt.
    candidate = worktree / "candidate.py"
    candidate.write_text("candidate = 2\n")
    with pytest.raises(ValueError, match="must be clean"):
        _checked_lock(worktree, context, tmp_path, run_process_group)
    candidate.write_text("candidate = 1\n")
    unknown = worktree / ".ai/unknown.txt"
    unknown.write_text("unknown\n")
    with pytest.raises(ValueError, match="must be clean"):
        _checked_lock(worktree, context, tmp_path, run_process_group)
    unknown.unlink()
    lock = worktree / "uv.lock"
    lock.unlink()
    lock.symlink_to(repo / "candidate.py")
    with pytest.raises(ValueError, match=r"must be clean|unsafe audit input"):
        _checked_lock(worktree, context, tmp_path, run_process_group)
    lock.unlink()
    lock.write_text("locked\n")
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-qm",
        "head moved",
        cwd=worktree,
    )
    with pytest.raises(ValueError, match="HEAD changed"):
        _checked_lock(worktree, context, tmp_path, run_process_group)
