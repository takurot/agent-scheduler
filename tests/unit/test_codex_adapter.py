from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from subsched.agents.codex import (
    CodexProbeConfig,
    CodexProbeSafetyError,
    build_codex_exec_argv,
    parse_codex_jsonl,
    run_codex_probe,
)
from subsched.models import AgentResultKind

FIXTURES = Path(__file__).parents[1] / "fixtures" / "codex"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_codex_argv_is_non_interactive_ephemeral_and_workspace_scoped(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    schema = tmp_path / "result.schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(executable=executable, cwd=tmp_path, output_schema=schema)

    argv = build_codex_exec_argv(config)

    assert argv == (
        str(executable),
        "--ask-for-approval",
        "never",
        "exec",
        "--strict-config",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--output-schema",
        str(schema),
        "-C",
        str(tmp_path),
        "--sandbox",
        "workspace-write",
        "--ephemeral",
        "-",
    )
    assert "--approve-for-me" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_success_fixture_normalizes_to_shared_agent_result() -> None:
    result = parse_codex_jsonl(_fixture("success.jsonl"), returncode=0)

    assert result.kind is AgentResultKind.PASS
    assert result.output == "codex completed"


def test_replays_sanitized_live_success_fixture() -> None:
    result = parse_codex_jsonl(_fixture("live-success.jsonl"), returncode=0)

    assert result.kind is AgentResultKind.PASS
    assert result.output == "codex completed"


def test_saved_cli_metadata_records_required_live_flags() -> None:
    version = _fixture("cli-version.txt")
    help_output = _fixture("cli-exec-help.txt")

    assert version == "codex-cli 0.147.0\n"
    for flag in (
        "--json",
        "--ask-for-approval",
        "--output-schema",
        "--sandbox",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
    ):
        assert flag in help_output


@pytest.mark.parametrize(
    ("fixture", "kind", "output"),
    [
        ("session-limit.jsonl", AgentResultKind.CAPACITY_SESSION, "codex session capacity"),
        ("weekly-limit.jsonl", AgentResultKind.CAPACITY_WEEKLY, "codex weekly capacity"),
        ("auth-error.jsonl", AgentResultKind.FAILURE, "codex authentication unavailable"),
        ("billing-error.jsonl", AgentResultKind.FAILURE, "codex billing mode unsafe"),
        ("approval-error.jsonl", AgentResultKind.FAILURE, "codex approval required"),
    ],
)
def test_failure_fixtures_are_classified(
    fixture: str, kind: AgentResultKind, output: str
) -> None:
    result = parse_codex_jsonl(_fixture(fixture), returncode=1)

    assert result.kind is kind
    assert result.output == output


def test_capacity_without_valid_reset_fails_closed() -> None:
    result = parse_codex_jsonl(_fixture("capacity-reset-unknown.jsonl"), returncode=1)

    assert result.kind is AgentResultKind.FAILURE
    assert result.reset_at is None
    assert result.output == "codex capacity reset unknown"


@pytest.mark.parametrize(
    "payload",
    [
        "not-json\n",
        '{"type":"future.event"}\n',
        '{"type":"turn.completed","usage":{}}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"not-json"}}\n'
        '{"type":"turn.completed","usage":{}}\n',
    ],
)
def test_malformed_or_unknown_events_fail_closed(payload: str) -> None:
    result = parse_codex_jsonl(payload, returncode=0)

    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "codex event stream malformed"


def test_capacity_fixture_has_timezone_aware_reset() -> None:
    result = parse_codex_jsonl(_fixture("session-limit.jsonl"), returncode=1)

    assert result.reset_at == datetime(2026, 8, 13, 2, 0, tzinfo=UTC)


def test_probe_requires_explicit_opt_in_and_verified_subscription(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"type": "object"}), encoding="utf-8")
    config = CodexProbeConfig(executable=executable, cwd=tmp_path, output_schema=schema)

    with pytest.raises(CodexProbeSafetyError, match="explicit opt-in"):
        run_codex_probe(config, "prompt", allow_live=False, subscription_billing_verified=True)
    with pytest.raises(CodexProbeSafetyError, match="subscription/billing"):
        run_codex_probe(config, "prompt", allow_live=True, subscription_billing_verified=False)


def test_config_rejects_non_executable_binary_and_invalid_schema(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    schema = tmp_path / "schema.json"
    schema.write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="executable"):
        CodexProbeConfig(executable=executable, cwd=tmp_path, output_schema=schema)

    executable.chmod(0o700)
    with pytest.raises(ValueError, match="schema"):
        CodexProbeConfig(executable=executable, cwd=tmp_path, output_schema=schema)


def test_spawn_failure_is_normalized_without_exposing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(executable=executable, cwd=tmp_path, output_schema=schema)

    def fail_spawn(*args: object, **kwargs: object) -> None:
        raise OSError("secret path")

    monkeypatch.setattr(subprocess, "Popen", fail_spawn)

    result = run_codex_probe(
        config,
        "safe fixture prompt",
        allow_live=True,
        subscription_billing_verified=True,
    )

    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "codex process unavailable"


def test_timeout_terminates_then_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(
        executable=executable,
        cwd=tmp_path,
        output_schema=schema,
        timeout_seconds=1,
        terminate_grace_seconds=1,
    )
    process = _TimedOutProcess()
    signals: list[tuple[int, int]] = []

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(os, "killpg", lambda pid, signal: signals.append((pid, signal)))

    result = run_codex_probe(
        config,
        "safe fixture prompt",
        allow_live=True,
        subscription_billing_verified=True,
    )

    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "codex execution timed out"
    assert signals == [(process.pid, 15), (process.pid, 9)]
    assert process.inputs == ["safe fixture prompt", None, None]


def test_process_exit_race_during_timeout_cleanup_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(executable=executable, cwd=tmp_path, output_schema=schema)
    process = _ExitedDuringCleanupProcess()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    def process_already_exited(pid: int, signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", process_already_exited)

    result = run_codex_probe(
        config,
        "safe fixture prompt",
        allow_live=True,
        subscription_billing_verified=True,
    )

    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "codex execution timed out"


def test_timeout_cleanup_that_cannot_reap_process_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(
        executable=executable,
        cwd=tmp_path,
        output_schema=schema,
        timeout_seconds=1,
        terminate_grace_seconds=1,
    )
    process = _NeverReapedProcess()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(os, "killpg", lambda pid, signal: None)

    result = run_codex_probe(
        config,
        "safe fixture prompt",
        allow_live=True,
        subscription_billing_verified=True,
    )

    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "codex timeout cleanup failed"
    assert process.timeouts == [1, 1, 1]


def test_process_lookup_cleanup_wait_remains_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(
        executable=executable,
        cwd=tmp_path,
        output_schema=schema,
        timeout_seconds=1,
        terminate_grace_seconds=1,
    )
    process = _NeverReapedProcess()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    def process_already_exited(pid: int, signal: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", process_already_exited)

    result = run_codex_probe(
        config,
        "safe fixture prompt",
        allow_live=True,
        subscription_billing_verified=True,
    )

    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "codex timeout cleanup failed"
    assert process.timeouts == [1, 1]


def test_timeout_cleanup_wait_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "codex"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(executable=executable, cwd=tmp_path, output_schema=schema)
    process = _ReapErrorProcess()

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(os, "killpg", lambda pid, signal: None)

    result = run_codex_probe(
        config,
        "safe fixture prompt",
        allow_live=True,
        subscription_billing_verified=True,
    )

    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "codex timeout cleanup failed"


class _TimedOutProcess:
    pid = 4312
    returncode = -9

    def __init__(self) -> None:
        self.inputs: list[str | None] = []

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        self.inputs.append(input)
        if len(self.inputs) < 3:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)
        return "", "secret stderr must not be returned"


class _ExitedDuringCleanupProcess(_TimedOutProcess):
    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        self.inputs.append(input)
        if len(self.inputs) == 1:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)
        return "", ""


class _NeverReapedProcess(_TimedOutProcess):
    def __init__(self) -> None:
        super().__init__()
        self.timeouts: list[float | None] = []

    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        self.inputs.append(input)
        self.timeouts.append(timeout)
        raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)


class _ReapErrorProcess(_TimedOutProcess):
    def communicate(
        self, input: str | None = None, timeout: float | None = None
    ) -> tuple[str, str]:
        self.inputs.append(input)
        if len(self.inputs) == 1:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)
        raise OSError("unsafe cleanup detail")
