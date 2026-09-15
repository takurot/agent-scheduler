from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from subsched.agents.claude import ClaudeCliMetadataError, parse_claude_cli_metadata
from subsched.agents.codex import (
    CodexApprovalMode,
    CodexCliMetadataError,
    parse_codex_cli_metadata,
)
from subsched.agents.isolation import native_isolation_failure, verify_native_isolation
from subsched.assumptions import REDACTED, SECRET_PATTERN
from subsched.config import AgentSettings, NativeIsolationConfig
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
    # #296: set only for the "claude"/"codex" checks, from the installed CLI's own
    # --help output. Consulted by validate_native_preflight() to fail closed, before any
    # dispatch, when an agent has an explicit stage/default model configured but the
    # installed CLI does not advertise a `--model` flag.
    supports_model_flag: bool = False


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
                supports_model_flag=metadata.supports_model_flag,
            )

        if name == "codex":
            v_proc = _safe_run([str(resolved), "--version"], run_cmd=run_cmd)
            h_proc = _safe_run([str(resolved), "exec", "--help"], run_cmd=run_cmd)
            if (
                v_proc.returncode != 0
                or h_proc.returncode != 0
                or not h_proc.stdout.strip()
            ):
                return PreflightCheckResult(
                    name=name,
                    found=True,
                    executable_path=resolved,
                    error="codex inspection failed (version or exec help exited nonzero)",
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
                supports_model_flag=metadata_codex.supports_model_flag,
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
    isolation_config: NativeIsolationConfig | None = None,
    executable_resolver: ExecutableResolver | None = None,
    run_cmd: RunCommand | None = None,
    # #296: per-agent model policy (config `agents.<provider>.models`), keyed by agent
    # name. Only consulted for agents that have an explicit stage/default model
    # configured; unset (None, the default) preserves pre-#296 behavior exactly.
    agents: Mapping[str, AgentSettings] | None = None,
) -> PreflightReport:
    """Validate installed CLI capabilities before task discovery and dispatch.

    Ensures git, gh, and only the configured/enabled agents are present and compatible
    with NativeWorker's execution requirements.
    """
    resolver = executable_resolver or _default_resolver
    runner = run_cmd or subprocess.run

    enabled = tuple(enabled_agents)

    # Core required tools
    targets: list[str] = ["git", "gh"]
    for agent in enabled:
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
            continue

        # #296: an agent with an explicit stage/default model configured must have its
        # installed CLI confirm `--model` support here, before any dispatch -- an
        # unsupported/ambiguous CLI fails closed instead of the flag being silently
        # ignored or rejected mid-dispatch, and never falls back to a different model.
        agent_settings = agents.get(name) if agents is not None else None
        if (
            agent_settings is not None
            and agent_settings.models.has_any_model
            and not res.supports_model_flag
        ):
            failures.append(
                f"{name} has a configured model but the installed CLI does not support "
                "a --model flag"
            )

    isolation_executable: Path | None = None
    if isolation_config is None:
        isolation_failure = native_isolation_failure()
    else:
        isolation_executable = resolver(isolation_config.runtime)
        isolation_failure = verify_native_isolation(
            isolation_config,
            enabled_agents=enabled,
            resolver=resolver,
            run_cmd=runner,
        )
    if isolation_failure is not None:
        checks.append(
            PreflightCheckResult(
                name="isolation",
                found=True,
                compatible=False,
                error=isolation_failure,
            )
        )
        failures.append(isolation_failure)
    else:
        checks.append(
            PreflightCheckResult(
                name="isolation",
                found=True,
                executable_path=isolation_executable,
                compatible=True,
                details=(
                    "container boundary verified; worker credential tier: "
                    "dedicated provider auth only"
                ),
            )
        )

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
