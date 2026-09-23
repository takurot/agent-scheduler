from __future__ import annotations

import json
import os
import shlex
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
from subsched.agents.isolation import (
    native_isolation_failure,
    probe_container_toolchain,
    verify_native_isolation,
)
from subsched.assumptions import REDACTED, SECRET_PATTERN
from subsched.config import AgentSettings, NativeIsolationConfig
from subsched.github.issues import diagnose_token

RunCommand = Callable[..., subprocess.CompletedProcess[str]]
ExecutableResolver = Callable[[str], Path | None]


def extract_command_binary(command_str: str) -> str | None:
    """Extract primary executable name from a shell command string (#364)."""
    try:
        parts = shlex.split(command_str.strip())
    except ValueError:
        return None
    for part in parts:
        if "=" in part and not part.startswith((".", "/")):
            continue
        return part
    return None


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
    # #313: whether the installed CLI advertises reasoning effort support.
    supports_effort_flag: bool = False


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
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    # Never expose environment secrets or model prompts
    return run_cmd(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=dict(env) if env is not None else {},
    )


_CLAUDE_SUBSCRIPTION_TYPES = frozenset({"pro", "max", "team", "business", "enterprise"})
_CLAUDE_METERED_ENV_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
    }
)


def _claude_settings_are_subscription_only(auth_dir: Path) -> bool:
    """Reject settings that can redirect an authenticated Claude CLI to metered usage."""
    for settings_path in (auth_dir / "settings.json", auth_dir / ".claude" / "settings.json"):
        if not settings_path.exists():
            continue
        if settings_path.is_symlink() or not settings_path.is_file():
            return False
        try:
            if settings_path.stat().st_size > 1_048_576:
                return False
            payload = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict) or "apiKeyHelper" in payload:
            return False
        configured_env = payload.get("env", {})
        if not isinstance(configured_env, dict):
            return False
        if _CLAUDE_METERED_ENV_KEYS.intersection(configured_env):
            return False
    return True


def _claude_subscription_status(stdout: str) -> bool:
    if len(stdout.encode("utf-8")) > 65_536:
        return False
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    if payload.get("loggedIn") is not True or payload.get("apiProvider") != "firstParty":
        return False
    auth_method = payload.get("authMethod")
    if auth_method == "oauth_token":
        return True
    return (
        auth_method == "claude.ai"
        and payload.get("subscriptionType") in _CLAUDE_SUBSCRIPTION_TYPES
    )


def probe_subscription_authentication(
    name: str,
    executable: Path,
    *,
    auth_dir: Path,
    run_cmd: RunCommand,
) -> PreflightCheckResult:
    """Verify an enabled CLI's local subscription login without invoking a model."""
    failure = f"{name} subscription authentication could not be verified"
    if name not in {"claude", "codex"}:
        return PreflightCheckResult(name=f"{name}-auth", found=False, error=failure)
    try:
        resolved_executable = executable.resolve(strict=True)
        resolved_auth = auth_dir.resolve(strict=True)
    except OSError:
        return PreflightCheckResult(name=f"{name}-auth", found=False, error=failure)
    if auth_dir.is_symlink() or not resolved_auth.is_dir():
        return PreflightCheckResult(name=f"{name}-auth", found=False, error=failure)

    env = {"HOME": str(resolved_auth), "NO_COLOR": "1"}
    if name == "codex":
        env["CODEX_HOME"] = str(resolved_auth)
        argv = [str(resolved_executable), "login", "status"]
    else:
        env["CLAUDE_CONFIG_DIR"] = str(resolved_auth)
        if not _claude_settings_are_subscription_only(resolved_auth):
            return PreflightCheckResult(name="claude-auth", found=True, error=failure)
        oauth_token = resolved_auth / "oauth-token"
        if oauth_token.exists():
            try:
                if (
                    oauth_token.is_symlink()
                    or not oauth_token.is_file()
                    or oauth_token.stat().st_size > 16_384
                ):
                    return PreflightCheckResult(name="claude-auth", found=True, error=failure)
                token = oauth_token.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                return PreflightCheckResult(name="claude-auth", found=True, error=failure)
            if not token:
                return PreflightCheckResult(name="claude-auth", found=True, error=failure)
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        argv = [str(resolved_executable), "auth", "status", "--json"]

    try:
        proc = _safe_run(argv, run_cmd=run_cmd, env=env)
    except (OSError, subprocess.SubprocessError):
        return PreflightCheckResult(name=f"{name}-auth", found=True, error=failure)
    authenticated = proc.returncode == 0 and (
        proc.stdout.strip() == "Logged in using ChatGPT"
        if name == "codex"
        else _claude_subscription_status(proc.stdout)
    )
    if not authenticated:
        return PreflightCheckResult(name=f"{name}-auth", found=True, error=failure)
    return PreflightCheckResult(
        name=f"{name}-auth",
        found=True,
        executable_path=resolved_executable,
        compatible=True,
        details="subscription authentication verified",
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
                supports_effort_flag=metadata.supports_effort_flag,
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
                supports_effort_flag=metadata_codex.supports_effort_flag,
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
    # #364: verification commands from verification.commands to probe against
    # the container image when container isolation is enabled.
    verification_commands: Sequence[str] = (),
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

        # #313: an agent with an explicit stage/default effort configured must have its
        # installed CLI confirm effort support here, before any dispatch.
        if (
            agent_settings is not None
            and agent_settings.effort.has_any_effort
            and not res.supports_effort_flag
        ):
            failures.append(
                f"{name} has a configured effort policy but the installed CLI does not support "
                "reasoning effort flags"
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

    # The operator flag is consent, not evidence. Verify the exact dedicated auth
    # directory that will be mounted into the isolated worker, using status commands
    # that do not invoke a model or consume provider capacity.
    if isolation_config is not None and isolation_failure is None:
        auth_by_agent = dict(isolation_config.auth)
        for agent in enabled:
            agent_check = next((check for check in checks if check.name == agent), None)
            auth_dir = auth_by_agent.get(agent)
            if (
                agent_check is None
                or not agent_check.compatible
                or agent_check.executable_path is None
            ):
                continue
            if auth_dir is None:
                auth_result = PreflightCheckResult(
                    name=f"{agent}-auth",
                    found=False,
                    error=f"{agent} subscription authentication could not be verified",
                )
            else:
                auth_result = probe_subscription_authentication(
                    agent,
                    agent_check.executable_path,
                    auth_dir=auth_dir,
                    run_cmd=runner,
                )
            checks.append(auth_result)
            if not auth_result.compatible:
                failures.append(auth_result.error or f"{agent} authentication check failed")

    # #364: when container isolation is active and verified, check that the container
    # image contains all executables required by verification.commands (e.g. cargo, pytest).
    if (
        isolation_config is not None
        and isolation_failure is None
        and isolation_config.backend == "container"
        and isolation_config.image is not None
        and verification_commands
    ):
        isolation_runtime = resolver(isolation_config.runtime) or Path(isolation_config.runtime)
        binaries_to_check: list[str] = []
        for cmd in verification_commands:
            binary = extract_command_binary(cmd)
            if binary and not binary.startswith((".", "/")) and binary not in binaries_to_check:
                binaries_to_check.append(binary)

        missing_toolchains: list[str] = []
        for binary in binaries_to_check:
            if not probe_container_toolchain(
                isolation_runtime,
                isolation_config.image,
                binary,
                run_cmd=runner,
            ):
                missing_toolchains.append(binary)

        if missing_toolchains:
            toolchain_error = (
                f"verification command toolchain(s) missing in container image "
                f"'{isolation_config.image}': {', '.join(missing_toolchains)}. "
                f"Pre-bake required toolchains (e.g. via examples/docker/Dockerfile.worker-rust) "
                f"into the worker container image."
            )
            checks.append(
                PreflightCheckResult(
                    name="isolation-toolchain",
                    found=False,
                    compatible=False,
                    error=toolchain_error,
                )
            )
            failures.append(toolchain_error)
        else:
            checks.append(
                PreflightCheckResult(
                    name="isolation-toolchain",
                    found=True,
                    compatible=True,
                    details=(
                        f"all verification toolchains available in worker container "
                        f"({', '.join(binaries_to_check)})"
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
