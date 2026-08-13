from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from contextlib import suppress
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
    output_limit_bytes: int = 1_048_576

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
        if self.output_limit_bytes <= 0:
            raise ValueError("Codex output limit must be positive")


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

    failure: dict[str, Any] | None = None
    terminal_index: int | None = None
    for index, event in enumerate(events):
        event_type = event.get("type")
        if event_type in {"error", "turn.failed"}:
            if terminal_index is not None:
                return _malformed_result()
            terminal_index = index
            failure = _failure_from_event(event)
            if failure is None:
                return _malformed_result()
        elif terminal_index is not None:
            return _malformed_result()

    if terminal_index is not None:
        if terminal_index != len(events) - 1:
            return _malformed_result()
        assert failure is not None
        if not _valid_failure_lifecycle(events):
            return _malformed_result()
        return _classify_failure(failure)
    if len(events) < 4:
        return _malformed_result()
    if events[0].get("type") != "thread.started" or events[1].get("type") != "turn.started":
        return _malformed_result()
    if events[-1].get("type") != "turn.completed":
        return _malformed_result()

    thread_started = events[0]
    completed = events[-1]
    if not isinstance(thread_started.get("thread_id"), str):
        return _malformed_result()
    if not isinstance(completed.get("usage"), dict):
        return _malformed_result()

    final_message: str | None = None
    message_count = 0
    for event in events[2:-1]:
        event_type = event.get("type")
        if event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "agent_message":
                return _malformed_result()
            if not isinstance(item.get("text"), str):
                return _malformed_result()
            final_message = item["text"]
            message_count += 1
        else:
            return _malformed_result()

    if returncode != 0:
        return AgentResult(AgentResultKind.FAILURE, output="codex execution failed")
    if final_message is None or message_count != 1:
        return _malformed_result()
    return _parse_final_message(final_message)


def _failure_from_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = event.get("type")
    if event_type == "error":
        if not isinstance(event.get("message"), str):
            return None
        return event
    if event_type == "turn.failed":
        error = event.get("error")
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            return None
        return error
    return None


def _valid_failure_lifecycle(events: tuple[dict[str, Any], ...]) -> bool:
    terminal_type = events[-1].get("type")
    if terminal_type == "error":
        return len(events) == 1
    if terminal_type != "turn.failed":
        return False
    if len(events) == 1:
        return True
    return (
        len(events) == 2
        and events[0].get("type") == "thread.started"
        and isinstance(events[0].get("thread_id"), str)
    )


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
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return AgentResult(AgentResultKind.FAILURE, output="codex process unavailable")
    output_queue: queue.Queue[tuple[bytes, bool, bool]] = queue.Queue(maxsize=1)
    input_queue: queue.Queue[bool] = queue.Queue(maxsize=1)
    reader = threading.Thread(
        target=_read_bounded_output,
        args=(process, config.output_limit_bytes, output_queue),
        daemon=True,
    )
    writer = threading.Thread(
        target=_write_prompt,
        args=(process, prompt.encode("utf-8"), input_queue),
        daemon=True,
    )
    reader.start()
    writer.start()
    try:
        process.wait(timeout=config.timeout_seconds)
    except OSError:
        if not _stop_process_group(process, config.terminate_grace_seconds):
            return AgentResult(AgentResultKind.FAILURE, output="codex cleanup failed")
        return AgentResult(AgentResultKind.FAILURE, output="codex process unavailable")
    except subprocess.TimeoutExpired:
        cleaned_up = _stop_process_group(process, config.terminate_grace_seconds)
        if not cleaned_up:
            return AgentResult(AgentResultKind.FAILURE, output="codex timeout cleanup failed")
        return AgentResult(AgentResultKind.FAILURE, output="codex execution timed out")
    writer.join(timeout=config.terminate_grace_seconds)
    if writer.is_alive():
        _stop_process_group(process, config.terminate_grace_seconds)
        return AgentResult(AgentResultKind.FAILURE, output="codex input cleanup failed")
    try:
        input_failed = input_queue.get_nowait()
    except queue.Empty:
        input_failed = True
    if input_failed:
        _stop_process_group(process, config.terminate_grace_seconds)
        return AgentResult(AgentResultKind.FAILURE, output="codex process unavailable")
    reader.join(timeout=config.terminate_grace_seconds)
    if reader.is_alive():
        _stop_process_group(process, config.terminate_grace_seconds)
        return AgentResult(AgentResultKind.FAILURE, output="codex output cleanup failed")
    try:
        stdout, overflow, read_failed = output_queue.get_nowait()
    except queue.Empty:
        return AgentResult(AgentResultKind.FAILURE, output="codex output unavailable")
    if overflow:
        _stop_process_group(process, config.terminate_grace_seconds)
        return AgentResult(AgentResultKind.FAILURE, output="codex output limit exceeded")
    if read_failed:
        return AgentResult(AgentResultKind.FAILURE, output="codex output unavailable")
    try:
        payload = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return _malformed_result()
    return parse_codex_jsonl(payload, returncode=process.returncode)


def _write_prompt(
    process: subprocess.Popen[bytes],
    prompt: bytes,
    input_queue: queue.Queue[bool],
) -> None:
    if process.stdin is None:
        input_queue.put(True)
        return
    try:
        process.stdin.write(prompt)
        process.stdin.close()
    except (BrokenPipeError, OSError):
        input_queue.put(True)
        return
    input_queue.put(False)


def _read_bounded_output(
    process: subprocess.Popen[bytes],
    output_limit_bytes: int,
    output_queue: queue.Queue[tuple[bytes, bool, bool]],
) -> None:
    if process.stdout is None:
        output_queue.put((b"", False, True))
        return
    payload = b""
    try:
        while chunk := process.stdout.read(min(65_536, output_limit_bytes + 1 - len(payload))):
            payload += chunk
            if len(payload) > output_limit_bytes:
                with suppress(OSError):
                    os.killpg(process.pid, signal.SIGTERM)
                output_queue.put((b"", True, False))
                return
    except OSError:
        output_queue.put((b"", False, True))
        return
    output_queue.put((payload, False, False))


def _stop_process_group(process: subprocess.Popen[bytes], grace_seconds: float) -> bool:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return _bounded_reap(process, grace_seconds)
    except OSError:
        return False
    deadline = time.monotonic() + grace_seconds
    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(min(0.01, grace_seconds))
    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False
    return _bounded_reap(process, grace_seconds) and not _process_group_exists(process.pid)


def _process_group_exists(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _bounded_reap(process: subprocess.Popen[bytes], timeout_seconds: float) -> bool:
    try:
        process.wait(timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _codex_environment() -> dict[str, str]:
    allowed = {"CODEX_HOME", "HOME", "LANG", "LC_ALL", "NO_COLOR", "PATH", "TERM"}
    return {name: value for name, value in os.environ.items() if name in allowed}
