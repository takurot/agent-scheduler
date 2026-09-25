from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import subsched.cli as cli_module
import subsched.notifications as notifications_module
from subsched.cli import app
from subsched.github.issues import GitHubIssueSource
from subsched.preflight import PreflightCheckResult, PreflightReport

runner = CliRunner()


def _write_config(path: Path, *, notifications_enabled: bool) -> Path:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    config = path / "scheduler.yaml"
    config.write_text(
        f"""
github:
  repo: owner/project
  mode: all-open
  base_branch: main
agents:
  claude:
    enabled: true
  codex:
    enabled: false
notifications:
  enabled: {str(notifications_enabled).lower()}
  max_delivery_attempts: 2
""",
        encoding="utf-8",
    )
    return config


def _prepare_run(monkeypatch: pytest.MonkeyPatch, *, run_id: str) -> None:
    report = PreflightReport(
        checks=(
            PreflightCheckResult(
                "isolation",
                True,
                executable_path=Path("/usr/bin/docker"),
                compatible=True,
            ),
        ),
        passed=True,
        failure_reasons=(),
    )
    monkeypatch.setattr(cli_module, "validate_native_preflight", lambda **_: report)
    monkeypatch.setattr(GitHubIssueSource, "list_open", lambda self, repo, **kwargs: ())
    monkeypatch.setattr(cli_module.secrets, "token_hex", lambda _: run_id)


def _invoke_run(repository: Path, config: Path, *extra: str):
    return runner.invoke(
        app,
        [
            "--repository",
            str(repository),
            "run",
            "--config",
            str(config),
            *extra,
        ],
    )


def test_normal_run_writes_summary_and_enabled_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_run(monkeypatch, run_id="normalrun")
    config = _write_config(tmp_path, notifications_enabled=True)

    result = _invoke_run(
        tmp_path,
        config,
        "--allow-native",
        "--subscription-billing-verified",
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".ai/runtime/run_summaries/run-normalrun.json").is_file()
    assert (tmp_path / ".ai/runtime/notifications_delivered.jsonl").is_file()


def test_dry_run_writes_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_run(monkeypatch, run_id="dryrun")
    config = _write_config(tmp_path, notifications_enabled=True)

    result = _invoke_run(tmp_path, config, "--dry-run")

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".ai/runtime/run_summaries/run-dryrun.json").is_file()
    assert (tmp_path / ".ai/runtime/notifications_delivered.jsonl").is_file()


def test_interrupted_run_writes_summary_and_preserves_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_run(monkeypatch, run_id="interruptedrun")
    config = _write_config(tmp_path, notifications_enabled=True)
    monkeypatch.setattr(
        cli_module,
        "_run_watch_loop",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    result = _invoke_run(
        tmp_path,
        config,
        "--allow-native",
        "--subscription-billing-verified",
    )

    assert result.exit_code == 130, result.output
    assert (tmp_path / ".ai/runtime/run_summaries/run-interruptedrun.json").is_file()


def test_disabled_notifications_still_write_summary_without_outbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_run(monkeypatch, run_id="disabledrun")
    config = _write_config(tmp_path, notifications_enabled=False)

    result = _invoke_run(
        tmp_path,
        config,
        "--allow-native",
        "--subscription-billing-verified",
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / ".ai/runtime/run_summaries/run-disabledrun.json").is_file()
    assert not (tmp_path / ".ai/runtime/notifications_outbox.json").exists()


def test_notification_failure_is_nonfatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare_run(monkeypatch, run_id="failednotification")
    config = _write_config(tmp_path, notifications_enabled=True)
    monkeypatch.setattr(
        notifications_module,
        "write_run_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    result = _invoke_run(
        tmp_path,
        config,
        "--allow-native",
        "--subscription-billing-verified",
    )

    assert result.exit_code == 0, result.output
    assert "Run summary/notification generation failed (non-fatal)" in result.output
