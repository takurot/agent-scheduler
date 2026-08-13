from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from subsched.assumptions import (
    AssumptionDecision,
    AssumptionManifestError,
    create_record,
    redact_argv,
    run_live_probe,
)


def test_record_redacts_secrets_prompts_and_personal_paths(tmp_path: Path) -> None:
    personal_path = tmp_path / "private" / "prompt.txt"

    record = create_record(
        cli_name="claude",
        cli_version="1.2.3",
        argv=(
            "claude",
            "--api-key",
            "not-a-real-secret",
            "--prompt=private request body",
            str(personal_path),
            f"--config={personal_path}",
        ),
        timestamp=datetime(2026, 8, 13, 0, 0, tzinfo=UTC),
        exit_code=1,
        schema_hash="a" * 64,
        decision=AssumptionDecision.UNKNOWN,
        personal_roots=(tmp_path,),
    )

    assert record.argv == (
        "claude",
        "--api-key",
        "[REDACTED]",
        "--prompt=[REDACTED]",
        "[REDACTED_PATH]",
        "[REDACTED_PATH]",
    )
    serialized = str(record.to_dict())
    assert "not-a-real-secret" not in serialized
    assert "private request body" not in serialized
    assert str(tmp_path) not in serialized


def test_pass_requires_explicit_verification() -> None:
    with pytest.raises(AssumptionManifestError, match="verified"):
        create_record(
            cli_name="gh",
            cli_version="2.80.0",
            argv=("gh", "version"),
            timestamp=datetime(2026, 8, 13, 0, 0, tzinfo=UTC),
            exit_code=0,
            schema_hash="b" * 64,
            decision=AssumptionDecision.PASS,
            verified=False,
        )


def test_record_rejects_invalid_external_values() -> None:
    with pytest.raises(AssumptionManifestError, match="schema_hash"):
        create_record(
            cli_name="gh",
            cli_version="2.80.0",
            argv=("gh", "version"),
            timestamp=datetime(2026, 8, 13, tzinfo=UTC),
            exit_code=0,
            schema_hash="not-a-hash",
            decision=AssumptionDecision.UNKNOWN,
        )


def test_record_rejects_wrong_external_types() -> None:
    with pytest.raises(AssumptionManifestError, match="cli_name"):
        create_record(
            cli_name=1,  # type: ignore[arg-type]
            cli_version="2.80.0",
            argv=("gh", "version"),
            timestamp=datetime(2026, 8, 13, tzinfo=UTC),
            exit_code=0,
            schema_hash="d" * 64,
            decision=AssumptionDecision.UNKNOWN,
        )


def test_redaction_rejects_sensitive_flag_without_value() -> None:
    with pytest.raises(AssumptionManifestError, match="missing its value"):
        redact_argv(("gh", "--token"))


def test_redaction_removes_codex_positional_prompt_body() -> None:
    assert redact_argv(("codex", "exec", "private prompt body")) == (
        "codex",
        "exec",
        "[REDACTED]",
    )


def test_redaction_removes_claude_positional_prompt_body() -> None:
    assert redact_argv(("claude", "private prompt body")) == (
        "claude",
        "[REDACTED]",
    )


def test_redaction_preserves_model_value_before_claude_positional_prompt() -> None:
    assert redact_argv(("claude", "--model", "sonnet", "private prompt body")) == (
        "claude",
        "--model",
        "sonnet",
        "[REDACTED]",
    )


@pytest.mark.parametrize(
    "flag",
    ("--system-prompt", "--append-system-prompt"),
)
def test_redaction_removes_claude_system_prompt_body(flag: str) -> None:
    assert redact_argv(("claude", f"{flag}=private system instructions")) == (
        "claude",
        f"{flag}=[REDACTED]",
    )


def test_redaction_removes_foreign_personal_path() -> None:
    assert redact_argv(
        ("gh", "--config=/Users/example/private/config.yml"), personal_roots=()
    ) == ("gh", "[REDACTED_PATH]")


def test_live_probe_requires_explicit_opt_in() -> None:
    called = False

    def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with pytest.raises(AssumptionManifestError, match="explicit opt-in"):
        run_live_probe(("gh", "version"), allow_live=False, run=fake_run)

    assert called is False


def test_live_probe_uses_bounded_subprocess_when_opted_in(tmp_path: Path) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)

    def fake_run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="gh version 2.80.0", stderr="")

    result = run_live_probe(
        ("gh", "version"),
        allow_live=True,
        run=fake_run,
        executable_paths={"gh": executable},
    )

    assert result.returncode == 0
    assert calls == [
        (
            (str(executable), "version"),
            {
                "capture_output": True,
                "text": True,
                "timeout": 30,
                "check": False,
                "env": {},
            },
        )
    ]


def test_live_probe_requires_validated_absolute_executable_path() -> None:
    with pytest.raises(AssumptionManifestError, match="trusted executable"):
        run_live_probe(
            ("gh", "version"),
            allow_live=True,
            executable_paths={"gh": Path("relative/gh")},
        )


def test_live_probe_rejects_commands_outside_safe_probe_allowlist() -> None:
    with pytest.raises(AssumptionManifestError, match="not allowed"):
        run_live_probe(("gh", "issue", "delete", "1"), allow_live=True)


def test_live_probe_wraps_subprocess_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("subsched.assumptions.shutil.which", lambda _: None)
    executable = tmp_path / "gh"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)

    def failed_run(
        argv: tuple[str, ...], **_: object
    ) -> subprocess.CompletedProcess[str]:
        raise OSError("private process detail")

    with pytest.raises(AssumptionManifestError, match="could not be executed") as error:
        run_live_probe(
            ("gh", "version"),
            allow_live=True,
            run=failed_run,
            executable_paths={"gh": executable},
        )

    assert "private process detail" not in str(error.value)
