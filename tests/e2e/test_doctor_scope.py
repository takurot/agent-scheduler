import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from subsched.cli import app


def test_basic_doctor_without_native_prerequisites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("git", "gh"):
        executable = tools / name
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setattr(
        "shutil.which", lambda name: str(tools / name) if name in {"git", "gh"} else None
    )

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[-1] == "--version":
            return subprocess.CompletedProcess(argv, 0, "version 1\n", "")
        return subprocess.CompletedProcess(
            argv,
            0,
            "github.com\n  - Active account: true\n  - Token scopes: 'repo', 'workflow'\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", run)
    runner = CliRunner()
    result = runner.invoke(app, ["--repository", str(tmp_path), "doctor", "--scope", "basic"])
    assert result.exit_code == 0, result.output
    assert "Basic doctor checks passed" in result.output
    assert "broader than required" in result.output
    assert "isolation VERIFIED" not in result.output
    assert "isolation UNVERIFIED" not in result.output
    assert {Path(call[0]).name for call in calls} == {"git", "gh"}
    result = runner.invoke(app, ["--repository", str(tmp_path), "doctor"])
    assert result.exit_code == 1
    assert "MISSING" in result.output


@pytest.mark.parametrize("failure", ["git", "gh", "version", "auth", "config"])
def test_basic_doctor_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("git", "gh"):
        executable = tools / name
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        executable.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda name: None if name == failure else str(tools / name))

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        failed = failure == ("version" if argv[-1] == "--version" else "auth")
        return subprocess.CompletedProcess(
            argv, int(failed), "", "ghp_123456789012345678901234567890"
        )

    monkeypatch.setattr(subprocess, "run", run)
    if failure == "config":
        (tmp_path / "subsched.yaml").write_text("unknown: true\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["--repository", str(tmp_path), "doctor", "--scope", "basic"])
    assert result.exit_code == 1, result.output
    assert "passed" not in result.output
    assert "ghp_" not in result.output


def test_doctor_rejects_unknown_scope(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["--repository", str(tmp_path), "doctor", "--scope", "other"])
    assert result.exit_code == 2
    assert "basic" in result.output and "native" in result.output
