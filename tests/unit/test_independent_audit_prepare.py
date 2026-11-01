from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.agents.isolation import IsolationGitContext
from subsched.audit_inventory import expected_cache_paths
from subsched.audit_prepare import AuditUnavailable, prepare_independent_audit
from subsched.audit_provenance import AuthenticatedCache
from subsched.config import IndependentAuditConfig


def test_prepares_cache_only_after_commit_inventory_and_ci_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.audit_prepare as prepare_module

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "uv.lock").write_text("locked\n")
    (worktree / "pyproject.toml").write_text("project\n")
    (worktree / "scripts").mkdir()
    (worktree / "scripts/offline-build-requirements.txt").write_text("build\n")
    git_dir = tmp_path / "git"
    git_dir.mkdir()
    context = IsolationGitContext(git_dir, "a" * 40, git_dir)
    config = IndependentAuditConfig(
        "owner/repo", 11, 22, "b" * 40, "c" * 64, 33, 44, "pip-audit 2.10.1", "test", 900
    )
    items = [("demo", "1"), ("pip-audit", "2.10.1")]
    calls = []

    def runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        if "rev-parse" in request.argv:
            return ProcessExecutionResult(exit_code=0, stdout="a" * 40 + "\n")
        if "status" in request.argv:
            return ProcessExecutionResult(exit_code=0, stdout="")
        path = request.argv[-1].split(":", 1)[1]
        return ProcessExecutionResult(exit_code=0, stdout=(worktree / path).read_text())

    def derive(*args: object, **kwargs: object) -> list[tuple[str, str]]:
        return items

    def fetch(
        repo: str,
        run_id: int,
        artifact_id: int,
        policy: object,
        now: datetime,
        inventory: object,
        **kwargs: object,
    ) -> AuthenticatedCache:
        calls.append((repo, run_id, artifact_id, policy, now, inventory))
        return AuthenticatedCache(
            {path: b"authenticated" for path in expected_cache_paths(items)},
            now - timedelta(seconds=1), timedelta(minutes=15),
        )

    cache = prepare_independent_audit(
        worktree,
        context,
        config,
        "owner/repo",
        "image@sha256:" + "d" * 64,
        Path("/usr/bin/true"),
        tmp_path / "cache",
        gh=Path("/usr/bin/true"),
        runner=runner,
        derive=derive,
        fetch=fetch,
        now=datetime(2026, 11, 1, tzinfo=UTC),
    )
    assert len(calls) == 1 and cache.path.is_dir()
    assert {
        p.relative_to(cache.path).as_posix() for p in cache.path.rglob("*") if p.is_file()
    } == expected_cache_paths(items)

    def changed_head(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        if "rev-parse" in request.argv:
            return ProcessExecutionResult(exit_code=0, stdout="f" * 40 + "\n")
        return runner(request)

    with pytest.raises(AuditUnavailable):
        prepare_independent_audit(
            worktree,
            context,
            config,
            "owner/repo",
            "image@sha256:" + "d" * 64,
            Path("/usr/bin/true"),
            tmp_path / "other-cache",
            gh=Path("/usr/bin/true"),
            runner=changed_head,
            derive=derive,
            fetch=fetch,
            now=datetime(2026, 11, 1, tzinfo=UTC),
        )
    assert len(calls) == 1

    def unconfirmed_cleanup(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        return ProcessExecutionResult(exit_code=1, stdout="", cleanup_succeeded=False)

    with pytest.raises(AuditUnavailable) as failure:
        prepare_independent_audit(
            worktree,
            context,
            config,
            "owner/repo",
            "image@sha256:" + "d" * 64,
            Path("/usr/bin/true"),
            tmp_path / "cleanup-cache",
            gh=Path("/usr/bin/true"),
            runner=unconfirmed_cleanup,
            derive=derive,
            fetch=fetch,
            now=datetime(2026, 11, 1, tzinfo=UTC),
        )
    assert failure.value.cleanup_confirmed is False

    clock = [datetime(2026, 11, 1, tzinfo=UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            return clock[0]

    def expiring_fetch(*args: object, **kwargs: object) -> AuthenticatedCache:
        return AuthenticatedCache(
            {path: b"authenticated" for path in expected_cache_paths(items)},
            clock[0], timedelta(seconds=1),
        )

    original_materialize = prepare_module.materialize_audit_cache

    def slow_materialize(*args: object, **kwargs: object) -> None:
        original_materialize(*args, **kwargs)
        clock[0] += timedelta(seconds=1)

    monkeypatch.setattr(prepare_module, "datetime", Clock)
    monkeypatch.setattr(prepare_module, "materialize_audit_cache", slow_materialize)
    with pytest.raises(AuditUnavailable):
        prepare_independent_audit(
            worktree, context, config, "owner/repo", "image@sha256:" + "d" * 64,
            Path("/usr/bin/true"), tmp_path / "expired-cache", gh=Path("/usr/bin/true"),
            runner=runner, derive=derive, fetch=expiring_fetch,
        )


def test_missing_github_evidence_fails_closed(tmp_path: Path) -> None:
    # A missing trust source must fail before any verification cache is created.
    context = IsolationGitContext(tmp_path, "a" * 40, tmp_path)
    config = IndependentAuditConfig(
        "owner/repo", 11, 22, "b" * 40, "c" * 64, 33, 44, "pip-audit 2.10.1", "test", 900
    )
    with pytest.raises(AuditUnavailable):
        prepare_independent_audit(
            tmp_path,
            context,
            config,
            "other/repo",
            "image@sha256:" + "d" * 64,
            Path("/usr/bin/true"),
            tmp_path / "cache",
            gh=Path("/usr/bin/true"),
        )
    assert not (tmp_path / "cache").exists()
