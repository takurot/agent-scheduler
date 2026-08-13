from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from subsched.models import AgentResult, AgentResultKind, CapacityState

MAX_RESULT_BYTES = 1_000_000
REQUIRED_HEADLESS_FLAGS = frozenset(
    {
        "--print",
        "--output-format",
        "--permission-mode",
        "--no-session-persistence",
        "--strict-mcp-config",
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


@dataclass(frozen=True, slots=True)
class ClaudeProcessOutcome:
    exit_code: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False
    cleanup_succeeded: bool = True


@dataclass(frozen=True, slots=True)
class ClaudeExecutionPolicy:
    live_probe_opt_in: bool
    billing_mode: ClaudeBillingMode

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
    supports_max_turns = "--max-turns" in help_output
    return ClaudeCliMetadata(
        version=version_match.group(1),
        supports_json_output='"json"' in help_output,
        supports_permission_mode="dontAsk" in help_output,
        supports_max_turns=supports_max_turns,
        requires_scheduler_timeout=not supports_max_turns,
        dangerous_permission_bypass_available="--dangerously-skip-permissions" in help_output,
    )


def parse_claude_result(outcome: ClaudeProcessOutcome) -> AgentResult:
    if outcome.timed_out:
        if not outcome.cleanup_succeeded:
            return AgentResult(
                AgentResultKind.PROCESS_CLEANUP_FAILED,
                output="claude process-group cleanup failed",
            )
        return AgentResult(AgentResultKind.TIMEOUT, output="claude scheduler timeout")

    payload = _load_payload(outcome.stdout)
    if payload is not None and _is_success(payload, outcome.exit_code):
        return AgentResult(AgentResultKind.PASS, output="claude completed")

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
        value = json.loads(stdout)
    except (json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(value, dict):
        return None
    return value


def _is_success(payload: dict[str, Any], exit_code: int) -> bool:
    has_output = isinstance(payload.get("result"), str) or "structured_output" in payload
    return (
        exit_code == 0
        and payload.get("type") == "result"
        and payload.get("subtype") == "success"
        and payload.get("is_error") is False
        and has_output
        and isinstance(payload.get("num_turns"), int)
        and payload["num_turns"] >= 0
    )


def _is_structured_failure(payload: dict[str, Any]) -> bool:
    return (
        payload.get("type") == "result"
        and payload.get("is_error") is True
        and isinstance(payload.get("result"), str)
    )


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
