"""Read independent audit evidence through authenticated GitHub CLI requests.

The expected policy must come from an operator-controlled trust root, never the
task or the GitHub response.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.agents.process import run_process_group
from subsched.audit_provenance import (
    AuditPolicy,
    AuthenticatedCache,
    _unique_object,
    authenticate_audit_artifact,
    authenticate_cache_bundle,
)
from subsched.config import validate_repo
from subsched.github.issues import _github_environment

_LIMIT = 2 * 1024 * 1024
_WORKFLOW = ".github/workflows/independent-audit.yml"


class AuditTransportIsolationError(RuntimeError):
    """The trusted GitHub subprocess cleanup could not be confirmed."""


def _fetch_github_evidence(
    repo: str,
    run_id: int,
    artifact_id: int,
    policy: AuditPolicy,
    *,
    gh: Path,
    runner: Callable[[ProcessExecutionRequest], ProcessExecutionResult] = run_process_group,
) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes] | None:
    """Fetch fixed authenticated GitHub endpoints and check for run-attempt races."""
    try:
        validate_repo(repo)
        if (
            policy.target.get("repository") != repo
            or type(run_id) is not int
            or run_id <= 0
            or type(artifact_id) is not int
            or artifact_id <= 0
            or not gh.is_absolute()
            or not gh.is_file()
            or re.fullmatch(r"[0-9a-f]{40}", policy.workflow_commit) is None
        ):
            return None
        cwd = Path(tempfile.gettempdir()).resolve()
        env = _github_environment()
        env.pop("GH_HOST", None)
        env.pop("GH_HTTP_UNIX_SOCKET", None)
        env["GH_PROMPT_DISABLED"] = "1"

        def execute(argv: tuple[str, ...]) -> str:
            result = runner(
                ProcessExecutionRequest(
                    argv=argv, cwd=cwd, env=env, timeout_seconds=45, output_limit_bytes=_LIMIT
                )
            )
            if not result.cleanup_succeeded:
                raise AuditTransportIsolationError("GitHub audit process cleanup unconfirmed")
            if (
                result.exit_code != 0
                or result.timed_out
                or result.output_limit_exceeded
                or result.command_not_found
                or not result.cleanup_succeeded
            ):
                raise ValueError("GitHub request failed")
            return result.stdout

        def api(endpoint: str) -> dict[str, Any]:
            raw = execute((str(gh), "api", "--hostname", "github.com", endpoint))
            value = json.loads(raw, object_pairs_hook=_unique_object)
            if not isinstance(value, dict):
                raise ValueError("invalid GitHub response")
            return value

        prefix = f"repos/{repo}"
        run_path = f"{prefix}/actions/runs/{run_id}"
        latest = api(run_path)
        attempt = latest.get("run_attempt")
        if type(attempt) is not int or attempt <= 0 or latest.get("id") != run_id:
            return None
        current = api(f"{run_path}/attempts/{attempt}")
        identity_fields = (
            "id",
            "run_attempt",
            "workflow_id",
            "event",
            "status",
            "conclusion",
            "head_sha",
            "run_started_at",
        )

        def same_identity(other: dict[str, Any]) -> bool:
            return all(other.get(key) == latest.get(key) for key in identity_fields) and all(
                other.get(key, {}).get("id") == latest.get(key, {}).get("id")
                for key in ("repository", "head_repository")
            )

        if not same_identity(current):
            return None
        registered = api(f"{prefix}/actions/workflows/{policy.workflow_id}")
        if (
            registered.get("id") != policy.workflow_id
            or registered.get("path") != _WORKFLOW
            or registered.get("state") != "active"
        ):
            return None
        artifact = api(f"{prefix}/actions/artifacts/{artifact_id}")
        if artifact.get("id") != artifact_id:
            return None
        content = api(f"{prefix}/contents/{_WORKFLOW}?ref={policy.workflow_commit}")
        if content.get("type") != "file" or content.get("encoding") != "base64":
            return None
        encoded = content.get("content")
        if not isinstance(encoded, str) or len(encoded) > _LIMIT:
            return None
        workflow = base64.b64decode("".join(encoded.split()), validate=True)
        archive64 = execute(
            (
                "/bin/bash",
                "-o",
                "pipefail",
                "-c",
                '"$1" api --hostname github.com "$2" | /usr/bin/base64',
                "subsched-audit",
                str(gh),
                f"{prefix}/actions/artifacts/{artifact_id}/zip",
            )
        )
        archive = base64.b64decode("".join(archive64.split()), validate=True)
        final = api(run_path)
        if not same_identity(final):
            return None
        return current, artifact, workflow, archive
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        UnicodeError,
        RecursionError,
        binascii.Error,
    ):
        return None


def fetch_github_audit(
    repo: str,
    run_id: int,
    artifact_id: int,
    policy: AuditPolicy,
    now: datetime | None,
    *,
    gh: Path,
    runner: Callable[[ProcessExecutionRequest], ProcessExecutionResult] = run_process_group,
) -> str:
    """Classify a CI record; this alone does not establish a usable cache."""
    evidence = _fetch_github_evidence(repo, run_id, artifact_id, policy, gh=gh, runner=runner)
    if evidence is None:
        return "unavailable"
    run, artifact, workflow, archive = evidence
    return authenticate_audit_artifact(
        policy, run, artifact, workflow, archive, now or datetime.now(UTC)
    )


def fetch_github_cache(
    repo: str,
    run_id: int,
    artifact_id: int,
    policy: AuditPolicy,
    now: datetime | None,
    inventory: Sequence[tuple[str, str]],
    *,
    gh: Path,
    runner: Callable[[ProcessExecutionRequest], ProcessExecutionResult] = run_process_group,
) -> AuthenticatedCache | None:
    """Fetch cache bytes only for authenticated, clean, exact-scope evidence."""
    evidence = _fetch_github_evidence(repo, run_id, artifact_id, policy, gh=gh, runner=runner)
    if evidence is None:
        return None
    run, artifact, workflow, archive = evidence
    return authenticate_cache_bundle(
        policy, run, artifact, workflow, archive, now or datetime.now(UTC), inventory
    )
