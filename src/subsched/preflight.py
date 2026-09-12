from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from subsched.agents.claude import ClaudeCliMetadataError, parse_claude_cli_metadata
from subsched.agents.codex import (
    CodexApprovalMode,
    CodexCliMetadataError,
    parse_codex_cli_metadata,
)
from subsched.assumptions import REDACTED, SECRET_PATTERN
from subsched.github.issues import diagnose_token

RunCommand = Callable[..., subprocess.CompletedProcess[str]]
ExecutableResolver = Callable[[str], Path | None]


@dataclass(frozen=True, slots=True)
class PreflightCheckResult:
    name: str
    found: bool
    executable_path: Path | None = None
    version: str | None = None
    compatible: bool = False
    details: str = ""
    error: str | None = None
    # #291: set only for the "codex" check, so NativeWorker can be given the exact
    # same approval-flag variant doctor/preflight verified is safe for the installed
    # CLI, instead of the two independently re-deriving (and potentially disagreeing
    # on) which flag is safe to use.
    codex_approval_mode: CodexApprovalMode | None = None


@dataclass(frozen=True, slots=True)
class PreflightReport:
    checks: tuple[PreflightCheckResult, ...]
    passed: bool
    failure_reasons: tuple[str, ...]

    def get(self, name: str) -> PreflightCheckResult | None:
        for check in self.checks:
            if check.name == name:
                return check
        return None


def _default_resolver(command: str) -> Path | None:
    found = shutil.which(command)
    return Path(found) if found is not None else None


def _safe_run(
    argv: Sequence[str],
    *,
    run_cmd: RunCommand,
    timeout: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    # Never expose environment secrets or model prompts
    return run_cmd(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={},
    )


def probe_command_capabilities(
    name: str,
    executable: Path,
    *,
    run_cmd: RunCommand,
) -> PreflightCheckResult:
    """Probe an executable for version and required capabilities without consuming capacity."""
    try:
        resolved = executable.resolve(strict=True)
    except OSError as error:
        return PreflightCheckResult(
            name=name,
            found=False,
            error=f"executable could not be resolved: {error}",
        )

    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        return PreflightCheckResult(
            name=name,
            found=False,
            error=f"executable is not executable: {resolved}",
        )

    try:
        if name in {"git", "gh"}:
            proc = _safe_run([str(resolved), "--version"], run_cmd=run_cmd)
            if proc.returncode != 0:
                return PreflightCheckResult(
                    name=name,
                    found=True,
                    executable_path=resolved,
                    error=f"{name} --version exited with code {proc.returncode}",
                )
            stdout = SECRET_PATTERN.sub(REDACTED, proc.stdout.strip())
            return PreflightCheckResult(
                name=name,
                found=True,
                executable_path=resolved,
                version=stdout.splitlines()[0] if stdout else None,
                compatible=True,
                details=stdout.splitlines()[0] if stdout else "",
            )

        if name == "claude":
            v_proc = _safe_run([str(resolved), "--version"], run_cmd=run_cmd)
            h_proc = _safe_run([str(resolved), "--help"], run_cmd=run_cmd)
            if v_proc.returncode != 0 or h_proc.returncode != 0:
                return PreflightCheckResult(
                    name=name,
                    found=True,
                    executable_path=resolved,
                    error="claude inspection failed (version or help exited nonzero)",
                )
            v_out = SECRET_PATTERN.sub(REDACTED, v_proc.stdout)
            h_out = SECRET_PATTERN.sub(REDACTED, h_proc.stdout)
            metadata = parse_claude_cli_metadata(version_output=v_out, help_output=h_out)
            return PreflightCheckResult(
                name=name,
                found=True,
                executable_path=resolved,
                version=metadata.version,
                compatible=True,
                details=f"version {metadata.version}, headless flags verified",
            )

        if name == "codex":
            v_proc = _safe_run([str(resolved), "--version"], run_cmd=run_cmd)
            h_proc = _safe_run([str(resolved), "exec", "--help"], run_cmd=run_cmd)
            if h_proc.returncode != 0 or not h_proc.stdout.strip():
                # Fallback to top-level help
                h_proc = _safe_run([str(resolved), "--help"], run_cmd=run_cmd)

            if v_proc.returncode != 0 or h_proc.returncode != 0:
                return PreflightCheckResult(
                    name=name,
                    found=True,
                    executable_path=resolved,
                    error="codex inspection failed (version or help exited nonzero)",
                )
            v_out = SECRET_PATTERN.sub(REDACTED, v_proc.stdout)
            h_out = SECRET_PATTERN.sub(REDACTED, h_proc.stdout)
            metadata_codex = parse_codex_cli_metadata(version_output=v_out, help_output=h_out)
            return PreflightCheckResult(
                name=name,
                found=True,
                executable_path=resolved,
                version=metadata_codex.version,
                compatible=True,
                details=f"version {metadata_codex.version}, headless flags verified",
                codex_approval_mode=metadata_codex.approval_mode,
            )

        return PreflightCheckResult(
            name=name,
            found=True,
            executable_path=resolved,
            error=f"unsupported pre-flight target: {name}",
        )
    except (ClaudeCliMetadataError, CodexCliMetadataError) as error:
        return PreflightCheckResult(
            name=name,
            found=True,
            executable_path=resolved,
            error=str(error),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return PreflightCheckResult(
            name=name,
            found=True,
            executable_path=resolved,
            error=f"inspection execution failed: {error}",
        )


def validate_native_preflight(
    *,
    enabled_agents: Iterable[str],
    write_policy_requires_auth: bool = False,
    executable_resolver: ExecutableResolver | None = None,
    run_cmd: RunCommand | None = None,
) -> PreflightReport:
    """Validate installed CLI capabilities before task discovery and dispatch.

    Ensures git, gh, and only the configured/enabled agents are present and compatible
    with NativeWorker's execution requirements.
    """
    resolver = executable_resolver or _default_resolver
    runner = run_cmd or subprocess.run

    # Core required tools
    targets: list[str] = ["git", "gh"]
    for agent in enabled_agents:
        if agent not in targets:
            targets.append(agent)

    checks: list[PreflightCheckResult] = []
    failures: list[str] = []

    for name in targets:
        exe_path = resolver(name)
        if exe_path is None:
            res = PreflightCheckResult(
                name=name,
                found=False,
                error=f"required command '{name}' is not installed or not in PATH",
            )
            checks.append(res)
            failures.append(res.error or f"missing {name}")
            continue

        res = probe_command_capabilities(name, exe_path, run_cmd=runner)
        checks.append(res)
        if not res.compatible:
            failures.append(f"{name} compatibility check failed: {res.error}")

    # Validate GitHub token when write policy requires authentication
    if write_policy_requires_auth:
        gh_check = next((c for c in checks if c.name == "gh"), None)
        if gh_check and gh_check.compatible:
            diagnosis = diagnose_token()
            if not diagnosis.authenticated:
                failures.append(
                    "GitHub authentication required by write policy (push/create_pr) "
                    "but gh is not authenticated"
                )

    return PreflightReport(
        checks=tuple(checks),
        passed=len(failures) == 0,
        failure_reasons=tuple(failures),
    )
