from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from typer.testing import CliRunner

from subsched.cli import app
from subsched.config import (
    GitHubConfig,
    IndependentAuditConfig,
    NativeIsolationConfig,
    SchedulerConfig,
    VerificationConfig,
)


def test_audit_request_emits_target_and_inventory_without_dispatch(
    tmp_path: Path, monkeypatch
) -> None:
    import subsched.agents.isolation as isolation
    import subsched.audit_prepare as audit
    import subsched.cli as cli

    trust = IndependentAuditConfig(
        "owner/repo", 11, 22, "b" * 40, "c" * 64, None, None, "pip-audit 2.10.1", "test", 900
    )
    cfg = replace(
        SchedulerConfig(),
        github=GitHubConfig(repo="owner/repo"),
        verification=VerificationConfig(independent_audit=trust),
        isolation=NativeIsolationConfig(backend="container", image="image@sha256:" + "d" * 64),
    )
    monkeypatch.setattr(cli, "_load_effective_config", lambda *args: cfg)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/true")
    monkeypatch.setattr(isolation, "prepare_verification_git", lambda worktree, root: object())
    monkeypatch.setattr(
        audit,
        "build_audit_target",
        lambda *args, **kwargs: ({"repository": "owner/repo", "commit": "a" * 40}, [("demo", "1")]),
    )
    result = CliRunner().invoke(
        app, ["--repository", str(tmp_path), "audit-request", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "target": {"repository": "owner/repo", "commit": "a" * 40},
        "inventory": [{"name": "demo", "version": "1"}],
    }


def test_run_with_missing_audit_evidence_stops_before_discovery(
    tmp_path: Path, monkeypatch
) -> None:
    import subsched.cli as cli

    trust = IndependentAuditConfig(
        "owner/repo", 11, 22, "b" * 40, "c" * 64, None, None, "pip-audit 2.10.1", "test", 900
    )
    cfg = replace(
        SchedulerConfig(),
        github=GitHubConfig(repo="owner/repo"),
        verification=VerificationConfig(independent_audit=trust),
        isolation=NativeIsolationConfig(backend="container", image="image@sha256:" + "d" * 64),
    )
    monkeypatch.setattr(cli, "_load_effective_config", lambda *args: cfg)

    def discovery(*args, **kwargs):
        raise AssertionError("must not discover or dispatch without audit evidence")

    monkeypatch.setattr(cli.GitHubIssueSource, "list_open", discovery)
    result = CliRunner().invoke(
        app,
        [
            "--repository",
            str(tmp_path),
            "run",
            "--issues",
            "1",
            "--allow-native",
            "--subscription-billing-verified",
        ],
    )
    assert result.exit_code != 0
    assert "independent audit" in result.output.lower()
