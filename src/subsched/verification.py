from __future__ import annotations

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


@dataclass(frozen=True, slots=True)
class VerificationReport:
    passed: bool
    gates: tuple[GateResult, ...]
    summary: str


def run_verification(
    worktree_dir: Path,
    commands: tuple[str, ...],
    env: dict[str, str] | None = None,
    timeout_seconds: float = 120.0,
    output_limit_bytes: int = 524288,
) -> VerificationReport:
    """Execute verification commands in worktree using isolated process runner."""
    clean_env = filter_environment(env or {}, allowlist=COMMON_ENV_ALLOWLIST)
    gate_results: list[GateResult] = []
    all_passed = True

    for cmd in commands:
        argv = tuple(shlex.split(cmd))
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
            )
        )
        if not passed:
            break

    summary_lines = [
        f"{g.command}: {'PASS' if g.passed else 'FAIL (exit ' + str(g.exit_code) + ')'}"
        for g in gate_results
    ]
    return VerificationReport(
        passed=all_passed,
        gates=tuple(gate_results),
        summary="\n".join(summary_lines),
    )
