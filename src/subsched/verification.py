from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.process import (
    COMMON_ENV_ALLOWLIST,
    filter_environment,
    redact_sensitive_command_audit,
    run_process_group,
)

MAX_VERIFICATION_ERROR_CHARS = 2000


@dataclass(frozen=True, slots=True)
class GateResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    passed: bool
    timed_out: bool = False
    command_not_found: bool = False
    # #373: mirrors ProcessExecutionResult.cleanup_succeeded -- False means the gate
    # process group may still be running and the exit code must not be trusted as a PASS.
    cleanup_succeeded: bool = True


@dataclass(frozen=True, slots=True)
class VerificationReport:
    passed: bool
    gates: tuple[GateResult, ...]
    summary: str
    # #373: False when any executed gate could not confirm its process-group cleanup.
    # The Scheduler treats this as a terminal safety condition (NEEDS_HUMAN), never as
    # an ordinary gate failure that consumes the verification retry budget.
    cleanup_confirmed: bool = True


def _gate_summary(gate: GateResult) -> str:
    if not gate.cleanup_succeeded:
        return "FAIL (process cleanup unconfirmed)"
    if gate.passed:
        return "PASS"
    if gate.command_not_found:
        stderr = redact_sensitive_command_audit((gate.stderr,))[0]
        if len(stderr) > MAX_VERIFICATION_ERROR_CHARS:
            stderr = stderr[:MAX_VERIFICATION_ERROR_CHARS] + "... [truncated]"
        return f"FAIL (command not found: {stderr})"
    return f"FAIL (exit {gate.exit_code})"


def run_verification(
    worktree_dir: Path,
    commands: tuple[str, ...],
    env: dict[str, str] | None = None,
    timeout_seconds: float = 120.0,
    output_limit_bytes: int = 524288,
) -> VerificationReport:
    """Execute verification commands in worktree using isolated process runner."""
    source_env = dict(os.environ) if env is None else env
    clean_env = filter_environment(source_env, allowlist=COMMON_ENV_ALLOWLIST)
    gate_results: list[GateResult] = []
    all_passed = True
    all_cleanups_confirmed = True

    for cmd in commands:
        try:
            argv = tuple(shlex.split(cmd))
        except ValueError as err:
            all_passed = False
            gate_results.append(
                GateResult(
                    command=cmd,
                    exit_code=1,
                    stdout="",
                    stderr=f"malformed command (unbalanced quotes): {err}",
                    passed=False,
                )
            )
            break

        if not argv:
            continue
        req = ProcessExecutionRequest(
            argv=argv,
            cwd=worktree_dir,
            env=clean_env,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes,
        )
        res = run_process_group(req)
        # #373: an unconfirmed cleanup invalidates the gate even at exit 0 -- leftover
        # processes are a terminal safety condition, not a green result.
        passed = (
            res.exit_code == 0
            and not res.timed_out
            and not res.output_limit_exceeded
            and res.cleanup_succeeded
        )
        if not passed:
            all_passed = False
        if not res.cleanup_succeeded:
            all_cleanups_confirmed = False
        gate_results.append(
            GateResult(
                command=cmd,
                exit_code=res.exit_code,
                stdout=res.stdout,
                stderr=res.stderr,
                passed=passed,
                timed_out=res.timed_out,
                command_not_found=res.command_not_found,
                cleanup_succeeded=res.cleanup_succeeded,
            )
        )
        if not passed:
            break

    if not gate_results:
        return VerificationReport(
            passed=False,
            gates=(),
            summary="FAIL (no executable verification commands configured)",
        )

    summary_lines = [
        f"{g.command}: {_gate_summary(g)}" for g in gate_results
    ]
    return VerificationReport(
        passed=all_passed,
        gates=tuple(gate_results),
        summary="\n".join(summary_lines),
        cleanup_confirmed=all_cleanups_confirmed,
    )
