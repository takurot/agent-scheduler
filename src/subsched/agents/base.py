from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from subsched.models import AgentResult


class LineSink(Protocol):
    """Incremental consumer of newline-delimited stdout (#475)."""

    def feed(self, line: bytes) -> None: ...


@dataclass(frozen=True, slots=True)
class ProcessExecutionRequest:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    stdin_payload: bytes = b""
    timeout_seconds: float = 300.0
    grace_seconds: float = 2.0
    output_limit_bytes: int = 1_048_576
    # #141: invoked with elapsed seconds every heartbeat_interval_seconds while waiting
    # on a long-running subprocess, so an operator watching structured logs/CLI output
    # can distinguish "still running" from "stopped/hung" -- without busy-polling, since
    # the wait loop only wakes up every heartbeat_interval_seconds, not on a tight loop.
    heartbeat: Callable[[float], None] | None = None
    heartbeat_interval_seconds: float = 60.0
    # #306: indicates whether this process execution is performing a PLAN_REVIEW
    plan_review: bool = False
    # #475: when set, stdout is streamed line by line into the sink instead of buffered.
    # output_limit_bytes then bounds each line, and stdout_stream_limit_bytes bounds the
    # whole stream, so memory stays bounded while long valid streams can complete.
    stdout_sink: LineSink | None = None
    stdout_stream_limit_bytes: int = 67_108_864

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("argv must not be empty")
        if not self.cwd.is_absolute() or not self.cwd.is_dir() or self.cwd.is_symlink():
            raise ValueError("cwd must be an absolute, existing, non-symlink directory")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.grace_seconds <= 0:
            raise ValueError("grace_seconds must be positive")
        if self.output_limit_bytes <= 0:
            raise ValueError("output_limit_bytes must be positive")
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive")
        if self.stdout_stream_limit_bytes <= 0:
            raise ValueError("stdout_stream_limit_bytes must be positive")


@dataclass(frozen=True, slots=True)
class ProcessExecutionResult:
    exit_code: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False
    output_limit_exceeded: bool = False
    cleanup_succeeded: bool = True
    command_not_found: bool = False


class AgentAdapter(Protocol):
    def execute(self, request: ProcessExecutionRequest) -> AgentResult: ...
