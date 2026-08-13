from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from subsched.models import AgentResult, AgentResultKind


class CodexProbeSafetyError(RuntimeError):
    """Raised before execution when the live-provider safety gate is not satisfied."""


@dataclass(frozen=True, slots=True)
class CodexProbeConfig:
    executable: Path
    cwd: Path
    output_schema: Path
    timeout_seconds: float = 300
    terminate_grace_seconds: float = 5

    def __post_init__(self) -> None:
        _validate_absolute_file(self.executable, "Codex executable")
        if not os.access(self.executable, os.X_OK):
            raise ValueError("Codex executable must be executable")
        _validate_absolute_file(self.output_schema, "output schema")
        _validate_schema(self.output_schema)
        if not self.cwd.is_absolute() or not self.cwd.is_dir() or self.cwd.is_symlink():
            raise ValueError("Codex cwd must be an absolute, existing, non-symlink directory")
        if self.timeout_seconds <= 0 or self.terminate_grace_seconds <= 0:
            raise ValueError("Codex timeouts must be positive")


def _validate_absolute_file(path: Path, description: str) -> None:
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError(f"{description} must be an absolute, existing, non-symlink file")


def _validate_schema(path: Path) -> None:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Codex output schema must be valid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("Codex output schema must be a JSON object")


def build_codex_exec_argv(config: CodexProbeConfig) -> tuple[str, ...]:
    """Build the fixed, non-interactive probe command without putting the prompt in argv."""
    return (
        str(config.executable),
        "--ask-for-approval",
        "never",
        "exec",
        "--strict-config",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "--output-schema",
        str(config.output_schema),
        "-C",
        str(config.cwd),
        "--sandbox",
        "workspace-write",
        "--ephemeral",
        "-",
    )


def parse_codex_jsonl(payload: str, *, returncode: int) -> AgentResult:
    """Normalize a complete Codex JSONL event stream to the shared AgentResult contract."""
    events = _load_events(payload)
    if events is None:
        return _malformed_result()

    final_message: str | None = None
    completed = False
    failure: dict[str, Any] | None = None
    for event in events:
        event_type = event.get("type")
        if event_type == "thread.started":
            if not isinstance(event.get("thread_id"), str):
                return _malformed_result()
        elif event_type == "turn.started":
            continue
        elif event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                return _malformed_result()
            if not isinstance(item.get("text"), str):
                return _malformed_result()
            final_message = item["text"]
        elif event_type == "turn.completed":
            if not isinstance(event.get("usage"), dict):
                return _malformed_result()
            completed = True
        elif event_type == "error":
            if not isinstance(event.get("message"), str):
                return _malformed_result()
            failure = event
        elif event_type == "turn.failed":
            error = event.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                return _malformed_result()
            failure = error
        else:
            return _malformed_result()

    if failure is not None:
        return _classify_failure(failure)
    if returncode != 0:
        return AgentResult(AgentResultKind.FAILURE, output="codex execution failed")
    if not completed or final_message is None:
        return _malformed_result()
    return _parse_final_message(final_message)


def _load_events(payload: str) -> tuple[dict[str, Any], ...] | None:
    lines = tuple(line for line in payload.splitlines() if line.strip())
    if not lines:
        return None
    events: list[dict[str, Any]] = []
    try:
        for line in lines:
            value: Any = json.loads(line)
            if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                return None
            events.append(value)
    except json.JSONDecodeError:
        return None
    return tuple(events)


def _parse_final_message(message: str) -> AgentResult:
    try:
        value: Any = json.loads(message)
    except json.JSONDecodeError:
        return _malformed_result()
    if (
        not isinstance(value, dict)
        or set(value) != {"result", "summary"}
        or value.get("result") not in {"pass", "failure"}
        or not isinstance(value.get("summary"), str)
    ):
        return _malformed_result()
    if value["result"] == "pass":
        return AgentResult(AgentResultKind.PASS, output="codex completed")
    return AgentResult(AgentResultKind.FAILURE, output="codex reported failure")


def _classify_failure(error: dict[str, Any]) -> AgentResult:
    message = str(error["message"]).casefold()
    if "rate limit" in message or "usage limit" in message:
        reset_at = _parse_reset_at(error.get("reset_at"))
        if reset_at is None:
            return AgentResult(AgentResultKind.FAILURE, output="codex capacity reset unknown")
        if "weekly" in message:
            return AgentResult(
                AgentResultKind.CAPACITY_WEEKLY,
                reset_at=reset_at,
                output="codex weekly capacity",
            )
        return AgentResult(
            AgentResultKind.CAPACITY_SESSION,
            reset_at=reset_at,
            output="codex session capacity",
        )
    if any(term in message for term in ("authentication", "unauthorized", "login")):
        return AgentResult(AgentResultKind.FAILURE, output="codex authentication unavailable")
    if any(term in message for term in ("billing", "payment", "credits")):
        return AgentResult(AgentResultKind.FAILURE, output="codex billing mode unsafe")
    if "approval" in message:
        return AgentResult(AgentResultKind.FAILURE, output="codex approval required")
    return AgentResult(AgentResultKind.FAILURE, output="codex execution failed")


def _parse_reset_at(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        reset_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return reset_at if reset_at.tzinfo is not None else None


def _malformed_result() -> AgentResult:
    return AgentResult(AgentResultKind.FAILURE, output="codex event stream malformed")


def run_codex_probe(
    config: CodexProbeConfig,
    prompt: str,
    *,
    allow_live: bool,
    subscription_billing_verified: bool,
) -> AgentResult:
    """Run one explicitly authorized probe and clean up its process group on timeout."""
    if not allow_live:
        raise CodexProbeSafetyError("Codex live probe requires explicit opt-in")
    if not subscription_billing_verified:
        raise CodexProbeSafetyError("Codex subscription/billing mode must be verified")
    if not prompt.strip():
        raise ValueError("Codex prompt must not be empty")

    try:
        process = subprocess.Popen(
            build_codex_exec_argv(config),
            cwd=config.cwd,
            env=_codex_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError:
        return AgentResult(AgentResultKind.FAILURE, output="codex process unavailable")
    try:
        stdout, _ = process.communicate(prompt, timeout=config.timeout_seconds)
    except subprocess.TimeoutExpired:
        cleaned_up = _stop_process_group(process, config.terminate_grace_seconds)
        if not cleaned_up:
            return AgentResult(AgentResultKind.FAILURE, output="codex timeout cleanup failed")
        return AgentResult(AgentResultKind.FAILURE, output="codex execution timed out")
    return parse_codex_jsonl(stdout, returncode=process.returncode)


def _stop_process_group(process: subprocess.Popen[str], grace_seconds: float) -> bool:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return _bounded_reap(process, grace_seconds)
    except OSError:
        return False
    try:
        process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
        return _bounded_reap(process, grace_seconds)
    except OSError:
        return False
    return True


def _bounded_reap(process: subprocess.Popen[str], timeout_seconds: float) -> bool:
    try:
        process.communicate(timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _codex_environment() -> dict[str, str]:
    allowed = {"CODEX_HOME", "HOME", "LANG", "LC_ALL", "NO_COLOR", "PATH", "TERM"}
    return {name: value for name, value in os.environ.items() if name in allowed}
