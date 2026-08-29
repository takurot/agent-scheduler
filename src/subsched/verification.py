from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.process import COMMON_ENV_ALLOWLIST, filter_environment, run_process_group


@dataclass(frozen=True, slots=True)
class GateResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    passed: bool
    timed_out: bool = False
    command_not_found: bool = False


@dataclass(frozen=True, slots=True)
class VerificationReport:
    passed: bool
    gates: tuple[GateResult, ...]
    summary: str


def _gate_summary(gate: GateResult) -> str:
    if gate.passed:
        return "PASS"
    if gate.command_not_found:
        return f"FAIL (command not found: {gate.stderr})"
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
        passed = res.exit_code == 0 and not res.timed_out and not res.output_limit_exceeded
        if not passed:
            all_passed = False
        gate_results.append(
            GateResult(
                command=cmd,
                exit_code=res.exit_code,
                stdout=res.stdout,
                stderr=res.stderr,
                passed=passed,
                timed_out=res.timed_out,
                command_not_found=res.command_not_found,
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
    )
