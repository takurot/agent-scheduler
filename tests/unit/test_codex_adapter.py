from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.codex import (
    CodexAgent,
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
        '{"type":"error","message":7}\n',
        '{"type":"error","message":"failure"}\n{"type":"turn.started"}\n',
        '{"type":"error","message":"failure"}\n'
        '{"type":"turn.failed","error":{"message":"failure"}}\n',
        '{"type":"future.event"}\n'
        '{"type":"turn.failed","error":{"message":"Weekly usage limit reached",'
        '"reset_at":"2026-08-20T00:00:00Z"}}\n',
        '{"type":"thread.started","thread_id":7}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"result\\":\\"pass\\",\\"summary\\":\\"unsafe\\"}"}}\n'
        '{"type":"turn.completed","usage":{}}\n',
        '{"type":"thread.started","thread_id":"fixture-thread"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"result\\":\\"pass\\",\\"summary\\":\\"unsafe\\"}"}}\n'
        '{"type":"turn.completed","usage":7}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"not-json"}}\n'
        '{"type":"turn.completed","usage":{}}\n',
    ],
)
def test_malformed_or_unknown_events_fail_closed(payload: str) -> None:
    result = parse_codex_jsonl(payload, returncode=0)

    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "codex event stream malformed"


@pytest.mark.parametrize(
    "payload",
    [
        '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"result\\":\\"pass\\",\\"summary\\":\\"unsafe\\"}"}}\n'
        '{"type":"turn.completed","usage":{}}\n',
        '{"type":"thread.started","thread_id":"fixture-thread"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"turn.completed","usage":{}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"result\\":\\"pass\\",\\"summary\\":\\"unsafe\\"}"}}\n',
        '{"type":"thread.started","thread_id":"fixture-thread"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"result\\":\\"pass\\",\\"summary\\":\\"unsafe\\"}"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"result\\":\\"pass\\",\\"summary\\":\\"unsafe\\"}"}}\n'
        '{"type":"turn.completed","usage":{}}\n',
        '{"type":"thread.started","thread_id":"fixture-thread"}\n'
        '{"type":"turn.started"}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"{\\"result\\":\\"pass\\",\\"summary\\":\\"unsafe\\"}"}}\n'
        '{"type":"turn.completed","usage":{}}\n'
        '{"type":"turn.completed","usage":{}}\n',
    ],
)
def test_incomplete_out_of_order_or_duplicate_event_lifecycle_fails_closed(
    payload: str,
) -> None:
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

    schema.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="output limit"):
        CodexProbeConfig(
            executable=executable,
            cwd=tmp_path,
            output_schema=schema,
            output_limit_bytes=0,
        )


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


def test_probe_terminates_process_group_when_stdout_exceeds_limit(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\nprintf '%04096d' 0\nsleep 5\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(
        executable=executable,
        cwd=tmp_path,
        output_schema=schema,
        timeout_seconds=2,
        terminate_grace_seconds=1,
        output_limit_bytes=128,
    )

    result = run_codex_probe(
        config,
        "safe fixture prompt",
        allow_live=True,
        subscription_billing_verified=True,
    )

    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "codex output limit exceeded"


def test_probe_timeout_includes_blocked_prompt_write(tmp_path: Path) -> None:
    executable = tmp_path / "codex"
    executable.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(
        executable=executable,
        cwd=tmp_path,
        output_schema=schema,
        timeout_seconds=0.05,
        terminate_grace_seconds=0.2,
    )

    started = time.monotonic()
    result = run_codex_probe(
        config,
        "x" * 8_000_000,
        allow_live=True,
        subscription_billing_verified=True,
    )

    assert time.monotonic() - started < 1
    assert result.kind is AgentResultKind.FAILURE
    assert result.output in {"codex execution timed out", "codex timeout cleanup failed"}


def test_probe_kills_descendant_that_holds_stdout_after_leader_exits(tmp_path: Path) -> None:
    child_pid_file = tmp_path / "child.pid"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "sleep 30 &\n"
        f"printf '%s' \"$!\" > {child_pid_file}\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(
        executable=executable,
        cwd=tmp_path,
        output_schema=schema,
        timeout_seconds=1,
        terminate_grace_seconds=0.1,
    )

    result = run_codex_probe(
        config,
        "safe fixture prompt",
        allow_live=True,
        subscription_billing_verified=True,
    )

    child_pid = int(child_pid_file.read_text(encoding="utf-8"))
    child_state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(child_pid)],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert not child_state or child_state.startswith("Z")
    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "codex output cleanup failed"


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
    group_alive = True

    def record_signal(pid: int, sent_signal: int) -> None:
        nonlocal group_alive
        if sent_signal == 0:
            if not group_alive:
                raise ProcessLookupError
            return
        signals.append((pid, sent_signal))
        if sent_signal == 9:
            group_alive = False

    monkeypatch.setattr(os, "killpg", record_signal)

    result = run_codex_probe(
        config,
        "safe fixture prompt",
        allow_live=True,
        subscription_billing_verified=True,
    )

    assert result.kind is AgentResultKind.FAILURE
    assert result.output == "codex execution timed out"
    assert signals == [(process.pid, 15), (process.pid, 9)]
    assert process.stdin_value == b"safe fixture prompt"


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
    assert process.timeouts == [1, 1]


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
    config = CodexProbeConfig(
        executable=executable,
        cwd=tmp_path,
        output_schema=schema,
        terminate_grace_seconds=0.1,
    )
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


class _RecordingInput(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.saved_value = b""

    def close(self) -> None:
        self.saved_value = self.getvalue()
        super().close()


class _TimedOutProcess:
    pid = 4312
    returncode = -9

    def __init__(self) -> None:
        self.stdin = _RecordingInput()
        self.stdout = io.BytesIO()
        self.timeouts: list[float | None] = []

    @property
    def stdin_value(self) -> bytes:
        return self.stdin.saved_value

    def wait(self, timeout: float | None = None) -> int:
        self.timeouts.append(timeout)
        if len(self.timeouts) < 2:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)
        return self.returncode


class _ExitedDuringCleanupProcess(_TimedOutProcess):
    def wait(self, timeout: float | None = None) -> int:
        self.timeouts.append(timeout)
        if len(self.timeouts) == 1:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)
        return self.returncode


class _NeverReapedProcess(_TimedOutProcess):
    def wait(self, timeout: float | None = None) -> int:
        self.timeouts.append(timeout)
        raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)


class _ReapErrorProcess(_TimedOutProcess):
    def wait(self, timeout: float | None = None) -> int:
        self.timeouts.append(timeout)
        if len(self.timeouts) == 1:
            raise subprocess.TimeoutExpired(cmd="codex", timeout=timeout or 0)
        raise OSError("unsafe cleanup detail")



def test_codex_agent_blocked_without_opt_in(tmp_path: Path) -> None:
    agent = CodexAgent(allow_live=False)
    req = ProcessExecutionRequest(
        argv=(sys.executable, "-c", "print('hello')"),
        cwd=tmp_path,
        env={},
    )
    result = agent.execute(req)
    assert result.kind is AgentResultKind.FAILURE
    assert "opted in" in result.output


def test_codex_agent_blocked_without_verified_billing(tmp_path: Path) -> None:
    agent = CodexAgent(allow_live=True, subscription_billing_verified=False)
    req = ProcessExecutionRequest(
        argv=(sys.executable, "-c", "print('hello')"),
        cwd=tmp_path,
        env={},
    )
    result = agent.execute(req)
    assert result.kind is AgentResultKind.UNKNOWN_BILLING


def test_codex_agent_executes_when_verified(tmp_path: Path) -> None:
    agent = CodexAgent(allow_live=True, subscription_billing_verified=True)
    msg = "{\"result\": \"pass\", \"summary\": \"all good\"}"
    script = (
        "import json; "
        "print(json.dumps({'type': 'thread.started', 'thread_id': 'th_123'})); "
        "print(json.dumps({'type': 'turn.started'})); "
        f"print(json.dumps({{'type': 'item.completed', "
        f"'item': {{'type': 'agent_message', 'text': {msg!r}}}}})); "
        "print(json.dumps({'type': 'turn.completed', 'usage': {}}))"
    )
    req = ProcessExecutionRequest(
        argv=(sys.executable, "-c", script),
        cwd=tmp_path,
        env={"PATH": ""},
    )
    result = agent.execute(req)
    assert result.kind is AgentResultKind.PASS


def test_codex_probe_config_validations(tmp_path: Path) -> None:
    exe = tmp_path / "codex"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")

    # Non-executable file
    non_exe = tmp_path / "non_exe"
    non_exe.write_text("", encoding="utf-8")
    non_exe.chmod(0o644)
    with pytest.raises(ValueError, match="must be executable"):
        CodexProbeConfig(executable=non_exe, cwd=tmp_path, output_schema=schema)

    # Relative cwd
    with pytest.raises(ValueError, match="Codex cwd must be"):
        CodexProbeConfig(executable=exe, cwd=Path("rel"), output_schema=schema)

    # Invalid timeouts
    with pytest.raises(ValueError, match="timeouts must be positive"):
        CodexProbeConfig(executable=exe, cwd=tmp_path, output_schema=schema, timeout_seconds=0)

    # Invalid output limit
    with pytest.raises(ValueError, match="output limit must be positive"):
        CodexProbeConfig(executable=exe, cwd=tmp_path, output_schema=schema, output_limit_bytes=0)

    # Non-dict schema
    from subsched.agents.codex import _validate_schema
    bad_schema = tmp_path / "bad.json"
    bad_schema.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        _validate_schema(bad_schema)


def test_codex_parse_jsonl_malformed_variations() -> None:
    # Less than 4 events
    assert parse_codex_jsonl("[]", returncode=0).kind is AgentResultKind.FAILURE

    # Bad header
    bad_header = [
        json.dumps({"type": "other"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
    assert parse_codex_jsonl("\n".join(bad_header), returncode=0).kind is AgentResultKind.FAILURE

    # Bad middle item
    bad_item = [
        json.dumps({"type": "thread.started", "thread_id": "th_1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "wrong_type"}}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
    assert parse_codex_jsonl("\n".join(bad_item), returncode=0).kind is AgentResultKind.FAILURE


def test_codex_parse_jsonl_returncode_error() -> None:
    msg = "{\"result\": \"pass\", \"summary\": \"all good\"}"
    payload = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "th_1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": msg}}),
            json.dumps({"type": "turn.completed", "usage": {}}),
        ]
    )
    res = parse_codex_jsonl(payload, returncode=1)
    assert res.kind is AgentResultKind.FAILURE
    assert "execution failed" in res.output


def test_validate_absolute_file_error(tmp_path: Path) -> None:
    from subsched.agents.codex import _validate_absolute_file

    with pytest.raises(ValueError, match="must be an absolute, existing, non-symlink file"):
        _validate_absolute_file(Path("relative/path"), "desc")
    with pytest.raises(ValueError, match="must be an absolute, existing, non-symlink file"):
        _validate_absolute_file(tmp_path / "non_existent", "desc")
    with pytest.raises(ValueError, match="must be an absolute, existing, non-symlink file"):
        _validate_absolute_file(tmp_path, "desc")


def test_codex_jsonl_additional_malformed_and_failure_branches() -> None:
    # terminal_index not at end
    events_terminal_not_end = [
        json.dumps({"type": "error", "message": "fail"}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
    res_not_end = parse_codex_jsonl("\n".join(events_terminal_not_end), returncode=0)
    assert res_not_end.kind is AgentResultKind.FAILURE

    # events[1] not turn.started
    pass_msg = json.dumps({"result": "pass", "summary": "ok"})
    events_bad_turn_start = [
        json.dumps({"type": "thread.started", "thread_id": "th_1"}),
        json.dumps({"type": "something_else"}),
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": pass_msg}}
        ),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
    res_bad_start = parse_codex_jsonl("\n".join(events_bad_turn_start), returncode=0)
    assert res_bad_start.kind is AgentResultKind.FAILURE

    # item agent_message without str text
    events_bad_text = [
        json.dumps({"type": "thread.started", "thread_id": "th_1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": 12345}}),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
    res_bad_text = parse_codex_jsonl("\n".join(events_bad_text), returncode=0)
    assert res_bad_text.kind is AgentResultKind.FAILURE

    # turn.failed with invalid error
    bad_err1 = json.dumps({"type": "turn.failed", "error": "not-dict"})
    assert parse_codex_jsonl(bad_err1, returncode=0).kind is AgentResultKind.FAILURE
    bad_err2 = json.dumps({"type": "turn.failed", "error": {"message": 123}})
    assert parse_codex_jsonl(bad_err2, returncode=0).kind is AgentResultKind.FAILURE

    # empty payload
    assert parse_codex_jsonl("", returncode=0).kind is AgentResultKind.FAILURE
    assert parse_codex_jsonl("   \n  \n", returncode=0).kind is AgentResultKind.FAILURE

    # final message not json or invalid schema
    events_final_not_json = [
        json.dumps({"type": "thread.started", "thread_id": "th_1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": "not-json"}}
        ),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
    res_not_json = parse_codex_jsonl("\n".join(events_final_not_json), returncode=0)
    assert res_not_json.kind is AgentResultKind.FAILURE

    bad_schema_text = json.dumps({"result": "invalid"})
    events_final_bad_schema = [
        json.dumps({"type": "thread.started", "thread_id": "th_1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {"type": "item.completed", "item": {"type": "agent_message", "text": bad_schema_text}}
        ),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
    res_bad_schema = parse_codex_jsonl("\n".join(events_final_bad_schema), returncode=0)
    assert res_bad_schema.kind is AgentResultKind.FAILURE

    # final message reported failure
    fail_summary_text = json.dumps({"result": "failure", "summary": "failed to fix"})
    events_final_failure = [
        json.dumps({"type": "thread.started", "thread_id": "th_1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": fail_summary_text},
            }
        ),
        json.dumps({"type": "turn.completed", "usage": {}}),
    ]
    res_final_fail = parse_codex_jsonl("\n".join(events_final_failure), returncode=0)
    assert res_final_fail.kind is AgentResultKind.FAILURE
    assert "reported failure" in res_final_fail.output

    # failure with approval required
    approval_err = json.dumps({"type": "error", "message": "approval required by user"})
    res_approval = parse_codex_jsonl(approval_err, returncode=0)
    assert res_approval.kind is AgentResultKind.FAILURE
    assert "approval required" in res_approval.output

    # failure with generic/unknown error
    generic_err = json.dumps({"type": "error", "message": "something unusual crashed"})
    res_generic = parse_codex_jsonl(generic_err, returncode=0)
    assert res_generic.kind is AgentResultKind.FAILURE
    assert "execution failed" in res_generic.output

    # _parse_reset_at with non-str
    from subsched.agents.codex import _parse_reset_at

    assert _parse_reset_at(12345) is None
    assert _parse_reset_at("invalid-date") is None


def test_run_codex_probe_validation_and_io_errors(tmp_path: Path) -> None:
    exe = tmp_path / "codex"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(executable=exe, cwd=tmp_path, output_schema=schema)

    # empty prompt
    with pytest.raises(ValueError, match="prompt must not be empty"):
        run_codex_probe(config, "   ", allow_live=True, subscription_billing_verified=True)

    # _write_prompt with stdin is None
    class FakeProcNoneIO:
        stdin = None
        stdout = None

    import queue

    from subsched.agents.codex import _read_bounded_output, _write_prompt

    in_q: queue.Queue[bool] = queue.Queue()
    _write_prompt(FakeProcNoneIO(), b"prompt", in_q)  # type: ignore[arg-type]
    assert in_q.get() is True

    # _read_bounded_output with stdout is None
    out_q: queue.Queue[tuple[bytes, bool, bool]] = queue.Queue()
    _read_bounded_output(FakeProcNoneIO(), 1024, out_q)  # type: ignore[arg-type]
    _, _, read_failed = out_q.get()
    assert read_failed is True


def test_codex_agent_execute_timeout_and_limit_branches() -> None:
    import subsched.agents.codex as codex_mod
    from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
    from subsched.agents.codex import CodexAgent

    agent = CodexAgent(allow_live=True, subscription_billing_verified=True)
    req = ProcessExecutionRequest(
        argv=("echo", "hi"),
        cwd=Path.cwd(),
        env={},
        stdin_payload=b"",
        timeout_seconds=10,
        grace_seconds=1,
        output_limit_bytes=1000,
    )

    # timed_out and cleanup failed
    def mock_run_timeout_cleanup_fail(request: Any) -> ProcessExecutionResult:
        return ProcessExecutionResult(
            exit_code=-15,
            stdout="",
            stderr="",
            timed_out=True,
            output_limit_exceeded=False,
            cleanup_succeeded=False,
        )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(codex_mod, "run_process_group", mock_run_timeout_cleanup_fail)
    res = agent.execute(req)
    assert res.kind is AgentResultKind.PROCESS_CLEANUP_FAILED

    # timed_out and cleanup succeeded
    def mock_run_timeout_cleanup_ok(request: Any) -> ProcessExecutionResult:
        return ProcessExecutionResult(
            exit_code=-15,
            stdout="",
            stderr="",
            timed_out=True,
            output_limit_exceeded=False,
            cleanup_succeeded=True,
        )

    monkeypatch.setattr(codex_mod, "run_process_group", mock_run_timeout_cleanup_ok)
    res = agent.execute(req)
    assert res.kind is AgentResultKind.TIMEOUT

    # output limit exceeded
    def mock_run_limit_exceeded(request: Any) -> ProcessExecutionResult:
        return ProcessExecutionResult(
            exit_code=0,
            stdout="",
            stderr="",
            timed_out=False,
            output_limit_exceeded=True,
            cleanup_succeeded=True,
        )

    monkeypatch.setattr(codex_mod, "run_process_group", mock_run_limit_exceeded)
    res = agent.execute(req)
    assert res.kind is AgentResultKind.FAILURE
    assert "output limit exceeded" in res.output
    monkeypatch.undo()


def test_stop_process_group_oserror() -> None:
    from subsched.agents.codex import _stop_process_group

    class FakeProc:
        pid = 999999

        def wait(self, timeout: float = 0) -> None:
            pass

    monkeypatch = pytest.MonkeyPatch()

    def mock_killpg(pid: int, sig: int) -> None:
        raise PermissionError("EPERM")

    monkeypatch.setattr(os, "killpg", mock_killpg)
    assert _stop_process_group(FakeProc(), grace_seconds=0.01) is False  # type: ignore[arg-type]
    monkeypatch.undo()


def test_run_codex_probe_popen_error(tmp_path: Path) -> None:
    exe = tmp_path / "codex"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    schema = tmp_path / "schema.json"
    schema.write_text("{}", encoding="utf-8")
    config = CodexProbeConfig(executable=exe, cwd=tmp_path, output_schema=schema)

    monkeypatch = pytest.MonkeyPatch()

    def mock_popen(*args: Any, **kwargs: Any) -> Any:
        raise OSError("cannot spawn")

    monkeypatch.setattr(subprocess, "Popen", mock_popen)
    res = run_codex_probe(config, "prompt", allow_live=True, subscription_billing_verified=True)
    assert res.kind is AgentResultKind.FAILURE
    assert "unavailable" in res.output
    monkeypatch.undo()


def test_codex_lifecycle_and_io_edge_cases() -> None:
    import queue

    from subsched.agents.codex import (
        _failure_from_event,
        _read_bounded_output,
        _valid_failure_lifecycle,
    )

    # _failure_from_event with unknown type
    assert _failure_from_event({"type": "custom_event"}) is None

    # _valid_failure_lifecycle with terminal_type not error or turn.failed
    assert _valid_failure_lifecycle(({"type": "turn.started"},)) is False

    # _read_bounded_output when read raises OSError
    class FakeStdoutError:
        def read(self, n: int) -> bytes:
            raise OSError("io error")

    class FakeProcWithStdoutError:
        stdout = FakeStdoutError()
        pid = 1234

    out_q: queue.Queue[tuple[bytes, bool, bool]] = queue.Queue()
    _read_bounded_output(FakeProcWithStdoutError(), 1024, out_q)  # type: ignore[arg-type]
    _, _, read_failed = out_q.get()
    assert read_failed is True
