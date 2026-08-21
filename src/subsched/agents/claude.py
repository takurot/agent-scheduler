from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.process import COMMON_ENV_ALLOWLIST, filter_environment, run_process_group
from subsched.models import AgentResult, AgentResultKind, CapacityState

MAX_RESULT_BYTES = 1_000_000
REQUIRED_HEADLESS_FLAGS = frozenset(
    {
        "--print",
        "--json-schema",
        "--output-format",
        "--permission-mode",
        "--safe-mode",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--tools",
    }
)


class ClaudeCliMetadataError(ValueError):
    pass


class ClaudeProbeBlocked(RuntimeError):
    pass


class ClaudeBillingMode(StrEnum):
    UNKNOWN = "UNKNOWN"
    SUBSCRIPTION_VERIFIED = "SUBSCRIPTION_VERIFIED"
    METERED = "METERED"


@dataclass(frozen=True, slots=True)
class ClaudeCliMetadata:
    version: str
    supports_json_output: bool
    supports_permission_mode: bool
    supports_max_turns: bool
    requires_scheduler_timeout: bool
    dangerous_permission_bypass_available: bool
    permission_mode_is_os_sandbox: bool
    native_execution_allowed: bool


@dataclass(frozen=True, slots=True)
class ClaudeProcessOutcome:
    exit_code: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False
    cleanup_succeeded: bool | None = None


@dataclass(frozen=True, slots=True)
class ClaudeExecutionPolicy:
    live_probe_opt_in: bool
    billing_mode: ClaudeBillingMode

    def __post_init__(self) -> None:
        if type(self.live_probe_opt_in) is not bool:
            raise TypeError("live probe opt-in must be a boolean")
        if not isinstance(self.billing_mode, ClaudeBillingMode):
            raise TypeError("billing mode must be a ClaudeBillingMode")

    @property
    def worker_state(self) -> CapacityState:
        if self.billing_mode is ClaudeBillingMode.UNKNOWN:
            return CapacityState.DISABLED_BILLING
        if self.billing_mode is ClaudeBillingMode.METERED:
            return CapacityState.DISABLED_BILLING
        return CapacityState.AVAILABLE if self.live_probe_opt_in else CapacityState.DISABLED

    @property
    def blocked_result(self) -> AgentResult:
        if self.billing_mode is ClaudeBillingMode.UNKNOWN:
            return AgentResult(AgentResultKind.UNKNOWN_BILLING, output="claude billing unknown")
        if self.billing_mode is ClaudeBillingMode.METERED:
            return AgentResult(
                AgentResultKind.BILLING_ERROR, output="claude metered billing disabled"
            )
        return AgentResult(AgentResultKind.FAILURE, output="claude live probe not opted in")

    def require_live_probe(self) -> None:
        if not self.live_probe_opt_in:
            raise ClaudeProbeBlocked("explicit live-probe opt-in is required")
        if self.billing_mode is ClaudeBillingMode.UNKNOWN:
            raise ClaudeProbeBlocked("billing mode is unknown")
        if self.billing_mode is ClaudeBillingMode.METERED:
            raise ClaudeProbeBlocked("metered billing is disabled")


def parse_claude_cli_metadata(*, version_output: str, help_output: str) -> ClaudeCliMetadata:
    version_match = re.fullmatch(r"\s*(\d+\.\d+\.\d+)(?:\s+\(Claude Code\))?\s*", version_output)
    if version_match is None:
        raise ClaudeCliMetadataError("unrecognized Claude CLI version")
    missing = sorted(flag for flag in REQUIRED_HEADLESS_FLAGS if flag not in help_output)
    if missing:
        raise ClaudeCliMetadataError("required Claude CLI flags are missing")
    supports_json_output = '"json"' in help_output
    supports_permission_mode = "dontAsk" in help_output
    if not supports_json_output or not supports_permission_mode:
        raise ClaudeCliMetadataError("required Claude CLI capabilities are missing")
    supports_max_turns = "--max-turns" in help_output
    return ClaudeCliMetadata(
        version=version_match.group(1),
        supports_json_output=supports_json_output,
        supports_permission_mode=supports_permission_mode,
        supports_max_turns=supports_max_turns,
        requires_scheduler_timeout=not supports_max_turns,
        dangerous_permission_bypass_available="--dangerously-skip-permissions" in help_output,
        permission_mode_is_os_sandbox=False,
        native_execution_allowed=False,
    )


def parse_claude_result(outcome: ClaudeProcessOutcome) -> AgentResult:
    if outcome.timed_out:
        if outcome.cleanup_succeeded is not True:
            return AgentResult(
                AgentResultKind.PROCESS_CLEANUP_FAILED,
                output="claude process-group cleanup failed",
            )
        return AgentResult(AgentResultKind.TIMEOUT, output="claude scheduler timeout")

    payload = _load_payload(outcome.stdout)
    if payload is not None and _is_success(payload, outcome.exit_code):
        return AgentResult(AgentResultKind.PASS, output="claude completed")
    if outcome.stdout and (payload is None or not _is_known_result(payload)):
        return AgentResult(AgentResultKind.UNKNOWN, output="claude result unknown")
    if payload is not None and (payload.get("subtype") == "success" or outcome.exit_code == 0):
        return AgentResult(AgentResultKind.UNKNOWN, output="claude result unknown")

    message = _failure_message(payload, outcome.stderr)
    lower_message = message.casefold()
    capacity_kind = _capacity_kind(lower_message)
    if capacity_kind is not None:
        reset_at = _reset_at(payload)
        if reset_at is None:
            return AgentResult(AgentResultKind.UNKNOWN, output="claude capacity reset unknown")
        return AgentResult(capacity_kind, reset_at=reset_at, output="claude capacity exhausted")
    if _contains_any(lower_message, ("overloaded", "temporarily unavailable", "try again")):
        return AgentResult(
            AgentResultKind.CAPACITY_TEMPORARY, output="claude temporarily unavailable"
        )
    if _contains_any(lower_message, ("authentication", "unauthorized", "run /login", "log in")):
        return AgentResult(AgentResultKind.AUTH_ERROR, output="claude authentication failed")
    if _contains_any(lower_message, ("credit balance", "billing", "payment required")):
        return AgentResult(AgentResultKind.BILLING_ERROR, output="claude billing rejected")
    if _contains_any(lower_message, ("permission denied", "not permitted", "approval required")):
        return AgentResult(AgentResultKind.PERMISSION_DENIED, output="claude permission denied")
    if payload is not None and _is_structured_failure(payload):
        return AgentResult(AgentResultKind.FAILURE, output="claude execution failed")
    return AgentResult(AgentResultKind.UNKNOWN, output="claude result unknown")


def _load_payload(stdout: str) -> dict[str, Any] | None:
    try:
        encoded_size = len(stdout.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if not stdout or encoded_size > MAX_RESULT_BYTES:
        return None
    try:
        value: Any = json.loads(stdout)
    except json.JSONDecodeError:
        return _load_jsonl_terminal(stdout)
    except UnicodeError:
        return None
    return value if isinstance(value, dict) else None


def _load_jsonl_terminal(stdout: str) -> dict[str, Any] | None:
    lines = tuple(line for line in stdout.splitlines() if line.strip())
    if not lines:
        return None
    events: list[dict[str, Any]] = []
    try:
        for line in lines:
            value: Any = json.loads(line)
            if not isinstance(value, dict) or not _is_known_stream_event(value):
                return None
            events.append(value)
    except (json.JSONDecodeError, UnicodeError):
        return None
    if events[-1].get("type") != "result":
        return None
    if any(event.get("type") == "result" for event in events[:-1]):
        return None
    return events[-1]


def _is_known_stream_event(event: dict[str, Any]) -> bool:
    event_type = event.get("type")
    if event_type == "system":
        return event.get("subtype") == "init"
    if event_type == "assistant":
        message = event.get("message")
        return isinstance(message, dict) and isinstance(message.get("content"), list)
    if event_type == "result":
        return _is_known_result(event)
    return False


def _is_known_result(payload: dict[str, Any]) -> bool:
    subtype = payload.get("subtype")
    has_known_shape = (
        payload.get("type") == "result"
        and subtype
        in {
            "success",
            "error_during_execution",
            "error_max_turns",
            "error_max_budget_usd",
            "error_max_structured_output_retries",
        }
        and type(payload.get("is_error")) is bool
        and type(payload.get("num_turns")) is int
        and payload["num_turns"] >= 0
    )
    if not has_known_shape:
        return False
    return (subtype == "success") is (payload["is_error"] is False)


def _is_success(payload: dict[str, Any], exit_code: int) -> bool:
    has_output = isinstance(payload.get("result"), str) or isinstance(
        payload.get("structured_output"), dict
    )
    return (
        _is_known_result(payload)
        and exit_code == 0
        and payload.get("type") == "result"
        and payload.get("subtype") == "success"
        and payload.get("is_error") is False
        and has_output
    )


def _is_structured_failure(payload: dict[str, Any]) -> bool:
    return (
        _is_known_result(payload)
        and payload.get("subtype") != "success"
        and payload.get("is_error") is True
        and isinstance(payload.get("result"), str)
    )


def cleanup_claude_process_group(process: subprocess.Popen[str], grace_seconds: float) -> bool:
    """Terminate and reap an isolated fake/native CLI process group within bounded waits."""
    if grace_seconds <= 0 or process.pid <= 0:
        return False
    try:
        if os.getpgid(process.pid) != process.pid:
            return False
    except OSError:
        return _bounded_reap(process, grace_seconds)
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return _bounded_reap(process, grace_seconds)
    except OSError:
        return False
    try:
        process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return False
    else:
        if _wait_process_group_gone(process.pid, grace_seconds):
            return True
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError:
        return False
    return _bounded_reap(process, grace_seconds)


def _bounded_reap(process: subprocess.Popen[str], timeout_seconds: float) -> bool:
    try:
        process.communicate(timeout=timeout_seconds)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return True


def _wait_process_group_gone(process_group: int, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(0.01, timeout_seconds))


def _failure_message(payload: dict[str, Any] | None, stderr: str) -> str:
    result = payload.get("result", "") if payload is not None else ""
    return " ".join(part for part in (result, stderr) if isinstance(part, str))


def _capacity_kind(message: str) -> AgentResultKind | None:
    if "weekly" in message and "limit" in message:
        return AgentResultKind.CAPACITY_WEEKLY
    if _contains_any(message, ("5-hour", "5 hour", "session limit", "usage limit")):
        return AgentResultKind.CAPACITY_SESSION
    return None


def _reset_at(payload: dict[str, Any] | None) -> datetime | None:
    if payload is None or not isinstance(payload.get("reset_at"), str):
        return None
    try:
        reset_at = datetime.fromisoformat(payload["reset_at"])
    except ValueError:
        return None
    return reset_at if reset_at.tzinfo is not None else None


def _contains_any(value: str, candidates: tuple[str, ...]) -> bool:
    return any(candidate in value for candidate in candidates)



class ClaudeAgent:
    """Claude Agent adapter adhering to AgentAdapter protocol."""

    def __init__(self, execution_policy: ClaudeExecutionPolicy | None = None) -> None:
        self.execution_policy = execution_policy or ClaudeExecutionPolicy(
            live_probe_opt_in=False,
            billing_mode=ClaudeBillingMode.UNKNOWN,
        )

    def execute(self, request: ProcessExecutionRequest) -> AgentResult:
        if self.execution_policy.billing_mode != ClaudeBillingMode.SUBSCRIPTION_VERIFIED:
            return self.execution_policy.blocked_result
        env = filter_environment(request.env, allowlist=COMMON_ENV_ALLOWLIST)
        proc_req = ProcessExecutionRequest(
            argv=request.argv,
            cwd=request.cwd,
            env=env,
            stdin_payload=request.stdin_payload,
            timeout_seconds=request.timeout_seconds,
            grace_seconds=request.grace_seconds,
            output_limit_bytes=request.output_limit_bytes,
        )
        res = run_process_group(proc_req)
        outcome = ClaudeProcessOutcome(
            exit_code=res.exit_code,
            stdout=res.stdout,
            stderr=res.stderr,
            timed_out=res.timed_out,
            cleanup_succeeded=res.cleanup_succeeded,
        )
        return parse_claude_result(outcome)
