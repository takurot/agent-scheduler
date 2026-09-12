from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from subsched.agents import SUPPORTED_AGENTS
from subsched.agents.codex import CodexApprovalMode
from subsched.cli import app
from subsched.config import ConfigError, load_config
from subsched.github.issues import GitHubIssueSource
from subsched.preflight import PreflightCheckResult, PreflightReport

runner = CliRunner()


def test_supported_agents_constant() -> None:
    """SUPPORTED_AGENTS must contain exactly claude and codex."""
    assert set(SUPPORTED_AGENTS) == {"claude", "codex"}


def test_unsupported_agent_name_raises_config_error(tmp_path: Path) -> None:
    """Config validation rejects typo or unknown agent names fail-fast."""
    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
agents:
  claude:
    enabled: true
  gpt4:
    enabled: true
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="unsupported agent 'gpt4'"):
        load_config(config_file)


def test_all_agents_disabled_raises_config_error(tmp_path: Path) -> None:
    """Config validation rejects configuration where all agents are disabled."""
    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
agents:
  claude:
    enabled: false
  codex:
    enabled: false
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="at least one agent must be enabled"):
        load_config(config_file)


def test_native_preflight_succeeds_when_disabled_codex_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native pre-flight succeeds when codex is disabled and missing from PATH."""
    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
  mode: all-open
  base_branch: main
agents:
  claude:
    enabled: true
  codex:
    enabled: false
""",
        encoding="utf-8",
    )

    observed_agents: list[tuple[str, ...]] = []

    def fake_preflight(*, enabled_agents, **kwargs):
        observed_agents.append(enabled_agents)
        return PreflightReport(checks=(), passed=True, failure_reasons=())

    monkeypatch.setattr("subsched.cli.validate_native_preflight", fake_preflight)
    monkeypatch.setattr(GitHubIssueSource, "list_open", lambda self, repo, **kwargs: ())

    res = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(config_file),
            "--allow-native",
            "--subscription-billing-verified",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "Pre-flight safety checks passed" in res.output
    assert observed_agents == [("claude",)]


def test_native_preflight_succeeds_when_disabled_claude_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native pre-flight succeeds when claude is disabled and missing from PATH."""
    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
  mode: all-open
  base_branch: main
agents:
  claude:
    enabled: false
  codex:
    enabled: true
""",
        encoding="utf-8",
    )

    observed_agents: list[tuple[str, ...]] = []

    def fake_preflight(*, enabled_agents, **kwargs):
        observed_agents.append(enabled_agents)
        return PreflightReport(checks=(), passed=True, failure_reasons=())

    monkeypatch.setattr("subsched.cli.validate_native_preflight", fake_preflight)
    monkeypatch.setattr(GitHubIssueSource, "list_open", lambda self, repo, **kwargs: ())

    res = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(config_file),
            "--allow-native",
            "--subscription-billing-verified",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "Pre-flight safety checks passed" in res.output
    assert observed_agents == [("codex",)]


def test_native_preflight_fails_when_enabled_agent_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native pre-flight fails when an enabled agent executable is missing from PATH."""
    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
  mode: all-open
  base_branch: main
agents:
  claude:
    enabled: true
  codex:
    enabled: false
""",
        encoding="utf-8",
    )

    failing_report = PreflightReport(
        checks=(PreflightCheckResult("claude", found=False),),
        passed=False,
        failure_reasons=("missing commands: claude",),
    )
    monkeypatch.setattr(
        "subsched.cli.validate_native_preflight",
        lambda *args, **kwargs: failing_report,
    )

    res = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(config_file),
            "--allow-native",
            "--subscription-billing-verified",
        ],
    )
    assert res.exit_code == 2, res.output
    assert "missing commands: claude" in res.output


def test_capacity_supplier_omits_disabled_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When an agent is disabled in config, capacity supplier does not probe it."""
    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
  mode: all-open
  base_branch: main
agents:
  claude:
    enabled: true
  codex:
    enabled: false
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(
        "subsched.cli.validate_native_preflight",
        lambda *args, **kwargs: PreflightReport(checks=(), passed=True, failure_reasons=()),
    )
    monkeypatch.setattr(GitHubIssueSource, "list_open", lambda self, repo, **kwargs: ())

    observed_agents: list[str] = []

    from subsched import cli as cli_mod

    def fake_run_watch_loop(scheduler, *, capacity_supplier, **kwargs):
        caps = capacity_supplier()
        for c in caps:
            observed_agents.append(c.agent)
        return False

    monkeypatch.setattr(cli_mod, "_run_watch_loop", fake_run_watch_loop)

    res = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(config_file),
            "--allow-native",
            "--subscription-billing-verified",
        ],
    )
    assert res.exit_code == 0, res.output
    # Only claude observations should be made, none for codex
    assert all(agent == "claude" for agent in observed_agents)


def test_run_passes_detected_codex_approval_mode_to_native_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#291: NativeWorker must be constructed with the exact approval-flag variant
    preflight detected for the installed Codex CLI, so `run --allow-native` and
    `doctor` never diverge on which argv is safe to execute."""
    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
  mode: all-open
  base_branch: main
agents:
  claude:
    enabled: false
  codex:
    enabled: true
""",
        encoding="utf-8",
    )

    codex_check = PreflightCheckResult(
        "codex",
        True,
        compatible=True,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
    monkeypatch.setattr(
        "subsched.cli.validate_native_preflight",
        lambda *args, **kwargs: PreflightReport(
            checks=(codex_check,), passed=True, failure_reasons=()
        ),
    )
    monkeypatch.setattr(GitHubIssueSource, "list_open", lambda self, repo, **kwargs: ())

    captured: dict[str, object] = {}

    class RecordingNativeWorker:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("subsched.cli.NativeWorker", RecordingNativeWorker)

    res = runner.invoke(
        app,
        [
            "run",
            "--config",
            str(config_file),
            "--allow-native",
            "--subscription-billing-verified",
        ],
    )
    assert res.exit_code == 0, res.output
    assert captured.get("codex_approval_mode") is CodexApprovalMode.APPROVE_FOR_ME
