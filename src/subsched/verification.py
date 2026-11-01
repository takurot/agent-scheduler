from __future__ import annotations

import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from subsched.agents.base import ProcessExecutionRequest
from subsched.agents.isolation import (
    cleanup_native_container,
    native_container_name,
    prepare_verification_git,
    wrap_verification_request,
)
from subsched.agents.process import (
    COMMON_ENV_ALLOWLIST,
    filter_environment,
    run_process_group,
)
from subsched.audit_prepare import AuditUnavailable
from subsched.config import IndependentAuditConfig, NativeIsolationConfig

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
    output_limit_exceeded: bool = False


@dataclass(frozen=True, slots=True)
class VerificationReport:
    passed: bool
    gates: tuple[GateResult, ...]
    summary: str
    # #373: False when any executed gate could not confirm its process-group cleanup.
    # The Scheduler treats this as a terminal safety condition (NEEDS_HUMAN), never as
    # an ordinary gate failure that consumes the verification retry budget.
    cleanup_confirmed: bool = True
    audit_unavailable: bool = False


def _gate_summary(gate: GateResult) -> str:
    if not gate.cleanup_succeeded:
        return "FAIL (process cleanup unconfirmed)"
    if gate.passed:
        return "PASS"
    if gate.timed_out:
        return "FAIL (timeout)"
    if gate.output_limit_exceeded:
        return "FAIL (output limit exceeded)"
    if gate.command_not_found:
        truncated = " ... [truncated]" if len(gate.stderr) > MAX_VERIFICATION_ERROR_CHARS else ""
        return f"FAIL (command not found: [REDACTED]{truncated})"
    stages = {
        "=== Syncing dependencies ===": "dependency sync",
        "=== Running Ruff linter ===": "ruff",
        "=== Running Mypy type checker ===": "mypy",
        "=== Running Pytest with Coverage Quality Gate (>=80% line, >=80% branch) ===": (
            "pytest/coverage"
        ),
        "=== Running Dependency Vulnerability Audit ===": "dependency audit",
    }
    reported_stage = next(
        (stages[line] for line in reversed(gate.stdout.splitlines()) if line in stages), None
    )
    suffix = f"; reported stage: {reported_stage}" if reported_stage else ""
    try:
        argv = shlex.split(gate.command)
    except ValueError:
        argv = []
    if len(argv) >= 2 and argv[:2] == ["uv", "run"]:
        argv = argv[2:]
        if argv and argv[0] == "--frozen":
            argv = argv[1:]
    if argv and argv[0] in {"pytest", "ruff", "mypy", "pip-audit"}:
        suffix += f"; invoked tool: {argv[0]}"
    return f"FAIL (exit {gate.exit_code}){suffix}"


def run_verification(
    worktree_dir: Path,
    commands: tuple[str, ...],
    env: dict[str, str] | None = None,
    timeout_seconds: float = 120.0,
    output_limit_bytes: int = 524288,
    isolation_config: NativeIsolationConfig | None = None,
    isolation_runtime_executable: Path | None = None,
    independent_audit: IndependentAuditConfig | None = None,
    audit_repository: str | None = None,
) -> VerificationReport:
    """Execute verification gates on the host or in the configured container boundary."""
    source_env = dict(os.environ) if env is None else env
    clean_env = filter_environment(source_env, allowlist=COMMON_ENV_ALLOWLIST)
    gate_results: list[GateResult] = []
    all_passed = True
    all_cleanups_confirmed = True
    if independent_audit is not None and (
        isolation_config is None
        or isolation_config.backend != "container"
        or isolation_config.image is None
        or isolation_runtime_executable is None
        or audit_repository is None
    ):
        return VerificationReport(
            passed=False,
            gates=(),
            summary="FAIL (independent audit requires isolated verification)",
            audit_unavailable=True,
        )

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
        snapshot_root: Path | None = None
        container_name: str | None = None
        container_runtime: Path | None = None
        audit_cache: Path | None = None
        git_context = None
        if isolation_config is not None and isolation_config.backend == "container":
            if isolation_runtime_executable is None:
                all_passed = False
                gate_results.append(
                    GateResult(
                        command=cmd,
                        exit_code=1,
                        stdout="",
                        stderr="verification container runtime was not supplied by preflight",
                        passed=False,
                    )
                )
                break
            container_runtime = isolation_runtime_executable
            container_name = native_container_name("verification")
            try:
                snapshot_root = Path(tempfile.mkdtemp(prefix="subsched-verification-")).resolve()
                git_context = prepare_verification_git(worktree_dir, snapshot_root / "git")
                if independent_audit is not None:
                    from subsched.audit_prepare import prepare_independent_audit

                    assert audit_repository is not None and isolation_config.image is not None
                    audit_cache = prepare_independent_audit(
                        worktree_dir,
                        git_context,
                        independent_audit,
                        audit_repository,
                        isolation_config.image,
                        isolation_runtime_executable,
                        snapshot_root / "audit-cache",
                    )
                    req = wrap_verification_request(
                        req,
                        config=isolation_config,
                        runtime_executable=isolation_runtime_executable,
                        container_name=container_name,
                        git_context=git_context,
                        audit_cache=audit_cache,
                    )
                else:
                    req = wrap_verification_request(
                        req,
                        config=isolation_config,
                        runtime_executable=isolation_runtime_executable,
                        container_name=container_name,
                        git_context=git_context,
                    )
            except AuditUnavailable as error:
                if snapshot_root is not None and error.cleanup_confirmed:
                    shutil.rmtree(snapshot_root)
                return VerificationReport(
                    passed=False,
                    gates=tuple(gate_results),
                    summary="FAIL (independent dependency audit unavailable or untrusted)",
                    cleanup_confirmed=error.cleanup_confirmed,
                    audit_unavailable=True,
                )
            except (ValueError, OSError) as error:
                if snapshot_root is not None:
                    shutil.rmtree(snapshot_root)
                all_passed = False
                gate_results.append(
                    GateResult(
                        command=cmd,
                        exit_code=1,
                        stdout="",
                        stderr=str(error),
                        passed=False,
                    )
                )
                break
        cleanup_failure: str | None = None
        audit_failure = None
        try:
            res = run_process_group(req)
            if (
                independent_audit is not None
                and res.exit_code == 0
                and not res.timed_out
                and not res.output_limit_exceeded
                and res.cleanup_succeeded
            ):
                from subsched.audit_prepare import prepare_independent_audit

                assert (
                    snapshot_root is not None
                    and git_context is not None
                    and isolation_config is not None
                    and isolation_config.image is not None
                    and isolation_runtime_executable is not None
                    and audit_repository is not None
                )
                try:
                    prepare_independent_audit(
                        worktree_dir,
                        git_context,
                        independent_audit,
                        audit_repository,
                        isolation_config.image,
                        isolation_runtime_executable,
                        snapshot_root / "audit-recheck",
                    )
                except AuditUnavailable as error:
                    audit_failure = error
        finally:
            if container_name is not None and container_runtime is not None:
                cleanup_failure = cleanup_native_container(
                    container_runtime,
                    container_name,
                    env=req.env,
                )
            if (
                snapshot_root is not None
                and cleanup_failure is None
                and (audit_failure is None or audit_failure.cleanup_confirmed)
            ):
                shutil.rmtree(snapshot_root)
        cleanup_succeeded = (
            res.cleanup_succeeded
            and cleanup_failure is None
            and (audit_failure is None or audit_failure.cleanup_confirmed)
        )
        # #373: an unconfirmed cleanup invalidates the gate even at exit 0 -- leftover
        # processes are a terminal safety condition, not a green result.
        passed = (
            res.exit_code == 0
            and not res.timed_out
            and not res.output_limit_exceeded
            and cleanup_succeeded
            and audit_failure is None
        )
        if not passed:
            all_passed = False
        if not cleanup_succeeded:
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
                cleanup_succeeded=cleanup_succeeded,
                output_limit_exceeded=res.output_limit_exceeded,
            )
        )
        if audit_failure is not None:
            return VerificationReport(
                passed=False,
                gates=tuple(gate_results),
                summary="FAIL (independent dependency audit changed or became unavailable)",
                cleanup_confirmed=cleanup_succeeded,
                audit_unavailable=True,
            )
        if not passed:
            break

    if not gate_results:
        return VerificationReport(
            passed=False,
            gates=(),
            summary="FAIL (no executable verification commands configured)",
        )

    summary_lines = [f"gate {index}: {_gate_summary(g)}" for index, g in enumerate(gate_results, 1)]
    return VerificationReport(
        passed=all_passed,
        gates=tuple(gate_results),
        summary="\n".join(summary_lines),
        cleanup_confirmed=all_cleanups_confirmed,
    )
