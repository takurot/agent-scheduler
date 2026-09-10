from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from subsched.preflight import (
    PreflightCheckResult,
    PreflightReport,
    probe_command_capabilities,
    validate_native_preflight,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


def test_probe_command_capabilities_claude_success(tmp_path: Path) -> None:
    exe = tmp_path / "claude"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    version_out = (FIXTURES / "claude" / "cli-version.txt").read_text(encoding="utf-8")
    help_out = (FIXTURES / "claude" / "cli-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=version_out, stderr="")
        if "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=help_out, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

    res = probe_command_capabilities("claude", exe, run_cmd=fake_run)
    assert res.compatible is True
    assert res.version == "2.1.229"
    assert "headless flags verified" in res.details


def test_probe_command_capabilities_codex_success(tmp_path: Path) -> None:
    exe = tmp_path / "codex"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    version_out = (FIXTURES / "codex" / "cli-version.txt").read_text(encoding="utf-8")
    help_out = (FIXTURES / "codex" / "cli-exec-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=version_out, stderr="")
        if "exec" in argv and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=help_out, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

    res = probe_command_capabilities("codex", exe, run_cmd=fake_run)
    assert res.compatible is True
    assert res.version == "0.147.0"
    assert "headless flags verified" in res.details


def test_probe_command_capabilities_rejects_missing_required_flags(tmp_path: Path) -> None:
    exe = tmp_path / "claude"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="2.1.229\n", stderr="")
        if "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="--print only\n", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    res = probe_command_capabilities("claude", exe, run_cmd=fake_run)
    assert res.compatible is False
    assert "required Claude CLI flags are missing" in (res.error or "")


def test_probe_command_capabilities_rejects_version_drift(tmp_path: Path) -> None:
    exe = tmp_path / "claude"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="unrecognized-output\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    res = probe_command_capabilities("claude", exe, run_cmd=fake_run)
    assert res.compatible is False
    assert "unrecognized Claude CLI version" in (res.error or "")


def test_validate_native_preflight_only_probes_enabled_agents(tmp_path: Path) -> None:
    claude_exe = tmp_path / "claude"
    claude_exe.write_text("", encoding="utf-8")
    claude_exe.chmod(0o755)

    git_exe = tmp_path / "git"
    git_exe.write_text("", encoding="utf-8")
    git_exe.chmod(0o755)

    gh_exe = tmp_path / "gh"
    gh_exe.write_text("", encoding="utf-8")
    gh_exe.chmod(0o755)

    def resolver(cmd: str) -> Path | None:
        if cmd == "claude":
            return claude_exe
        if cmd == "git":
            return git_exe
        if cmd == "gh":
            return gh_exe
        return None

    claude_v = (FIXTURES / "claude" / "cli-version.txt").read_text(encoding="utf-8")
    claude_h = (FIXTURES / "claude" / "cli-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        if name == "git":
            return subprocess.CompletedProcess(argv, 0, stdout="git version 2.40.0\n", stderr="")
        if name == "gh":
            return subprocess.CompletedProcess(argv, 0, stdout="gh version 2.50.0\n", stderr="")
        if name == "claude" and "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_v, stderr="")
        if name == "claude" and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_h, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    report = validate_native_preflight(
        enabled_agents=("claude",),
        executable_resolver=resolver,
        run_cmd=fake_run,
    )
    assert report.passed is True
    assert report.get("claude") is not None
    assert report.get("claude").compatible is True
    # Codex was not enabled, so it should not be in report checks
    assert report.get("codex") is None


def test_validate_native_preflight_probes_never_invoke_model_or_expose_credentials(
    tmp_path: Path,
) -> None:
    exe = tmp_path / "codex"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    captured_runs: list[dict[str, object]] = []

    def recording_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_runs.append({"argv": argv, "env": kwargs.get("env")})
        return subprocess.CompletedProcess(argv, 0, stdout="codex-cli 0.147.0\n", stderr="")

    probe_command_capabilities("codex", exe, run_cmd=recording_run)

    for run_info in captured_runs:
        argv = run_info["argv"]
        assert isinstance(argv, list)
        assert not any("sk-" in arg for arg in argv)
        # Probe commands must be --version or --help, never prompts or tasks
        assert any(flag in argv for flag in ("--version", "--help"))
        # Env should be empty to prevent credential leakage
        assert run_info["env"] == {}


def test_validate_native_preflight_fails_when_write_policy_requires_auth_and_gh_unauthenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_exe = tmp_path / "git"
    git_exe.write_text("", encoding="utf-8")
    git_exe.chmod(0o755)

    gh_exe = tmp_path / "gh"
    gh_exe.write_text("", encoding="utf-8")
    gh_exe.chmod(0o755)

    def resolver(cmd: str) -> Path | None:
        if cmd == "git":
            return git_exe
        if cmd == "gh":
            return gh_exe
        return None

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="version 1.0\n", stderr="")

    from subsched.github.issues import TokenDiagnosis
    monkeypatch.setattr(
        "subsched.preflight.diagnose_token",
        lambda: TokenDiagnosis(
            authenticated=False,
            scopes=(),
            can_discover=False,
            can_write=False,
            broad_scopes=(),
        ),
    )

    report = validate_native_preflight(
        enabled_agents=(),
        write_policy_requires_auth=True,
        executable_resolver=resolver,
        run_cmd=fake_run,
    )
    assert report.passed is False
    assert any("GitHub authentication required" in r for r in report.failure_reasons)


def test_doctor_and_run_share_capability_failure_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from subsched.cli import app

    runner = CliRunner()
    failing_report = PreflightReport(
        checks=(
            PreflightCheckResult("git", True, compatible=True),
            PreflightCheckResult("gh", True, compatible=True),
            PreflightCheckResult(
                "claude",
                True,
                compatible=False,
                error="required Claude CLI flags are missing",
            ),
        ),
        passed=False,
        failure_reasons=(
            "claude compatibility check failed: required Claude CLI flags are missing",
        ),
    )

    monkeypatch.setattr(
        "subsched.cli.validate_native_preflight",
        lambda *args, **kwargs: failing_report,
    )

    # doctor command check
    doctor_res = runner.invoke(app, ["doctor"])
    assert doctor_res.exit_code == 1
    assert "compatibility error" in doctor_res.output
    assert "required Claude CLI flags are missing" in doctor_res.output

    # run command check
    run_res = runner.invoke(
        app,
        [
            "run",
            "--repo",
            "owner/repo",
            "--issues",
            "1",
            "--allow-native",
            "--subscription-billing-verified",
        ],
    )
    assert run_res.exit_code == 2
    assert "Native execution pre-flight doctor check failed" in run_res.output
    assert "required Claude CLI flags are missing" in run_res.output


