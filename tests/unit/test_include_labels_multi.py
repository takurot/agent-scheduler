from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from subsched.cli import _format_effective_config_summary, _resolve_intent, app
from subsched.config import GitHubConfig, SchedulerConfig
from subsched.github.issues import GitHubIssueSource
from subsched.models import Issue

runner = CliRunner()


def test_github_adapter_passes_multiple_labels_to_gh_cli(
    monkeypatch: subprocess.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout="[]",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    GitHubIssueSource().list_open(
        "owner/project",
        labels=("ai-ready", "scheduler-ready"),
    )

    assert len(calls) == 1
    argv = calls[0]
    # Check that both labels are passed with --label flags (AND semantics in gh issue list)
    label_indices = [i for i, arg in enumerate(argv) if arg == "--label"]
    assert len(label_indices) == 2
    assert argv[label_indices[0] + 1] == "ai-ready"
    assert argv[label_indices[1] + 1] == "scheduler-ready"


def test_resolve_intent_preserves_multiple_include_labels() -> None:
    cfg = SchedulerConfig(
        github=GitHubConfig(
            repo="owner/project",
            mode="label",
            include_labels=("ai-ready", "backend"),
        )
    )
    intent = _resolve_intent(cfg=cfg, query=None, repo=None, label=None, issues=None)
    assert intent.labels == ("ai-ready", "backend")
    summary = _format_effective_config_summary(intent, dry_run=True, allow_native=False)
    assert any(
        "labels=ai-ready, backend" in line or "labels=ai-ready,backend" in line
        for line in summary
    )


def test_cli_discovery_with_multiple_include_labels_and_semantics(
    tmp_path: Path, monkeypatch: subprocess.MonkeyPatch
) -> None:
    # Issue 1: has both labels -> should be discovered
    # Issue 2: has only "ai-ready" -> should NOT be discovered
    # Issue 3: has both labels but also "blocked" -> should be excluded
    test_issues = (
        Issue(number=1, title="ready", labels=("ai-ready", "backend")),
        Issue(number=2, title="only-ai", labels=("ai-ready",)),
        Issue(number=3, title="blocked", labels=("ai-ready", "backend", "blocked")),
    )

    def fake_list_open(
        self: GitHubIssueSource,
        repo: str,
        *,
        label: str | None = None,
        labels: tuple[str, ...] = (),
        limit: int = 1000,
    ) -> tuple[Issue, ...]:
        return test_issues

    monkeypatch.setattr(GitHubIssueSource, "list_open", fake_list_open)

    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
  mode: label
  include_labels:
    - ai-ready
    - backend
  exclude_labels:
    - blocked
""",
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["--repository", str(tmp_path), "run", "--config", str(config_file), "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert "1 issue(s) discovered" in result.output
