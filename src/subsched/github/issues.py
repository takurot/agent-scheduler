from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from typing import Any, NamedTuple

from subsched.config import validate_repo
from subsched.models import Issue


class GitHubCliError(RuntimeError):
    pass


RunCommand = Callable[..., subprocess.CompletedProcess[str]]

# Scopes that grant mutation capability (push, PR/issue write, workflow dispatch, org/repo
# admin). Phase 1 discovery only reads issues, so any of these being present on the active
# token is broader than required and is surfaced as a doctor warning.
WRITE_CAPABLE_SCOPES = frozenset(
    {"repo", "public_repo", "workflow", "delete_repo", "admin:org", "admin:repo_hook"}
)


class TokenDiagnosis(NamedTuple):
    authenticated: bool
    scopes: tuple[str, ...]
    can_discover: bool
    can_write: bool
    broad_scopes: tuple[str, ...]


def diagnose_token(run: RunCommand | None = None) -> TokenDiagnosis:
    """Diagnose the active `gh` token's read/write capability without exposing its value.

    Never passes `--show-token`; only scope names from `gh auth status` are inspected.
    """
    argv = ["gh", "auth", "status", "--json", "hosts"]
    try:
        runner = run or subprocess.run
        result = runner(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=_github_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return TokenDiagnosis(
            authenticated=False, scopes=(), can_discover=False, can_write=False, broad_scopes=()
        )
    if result.returncode != 0:
        return TokenDiagnosis(
            authenticated=False, scopes=(), can_discover=False, can_write=False, broad_scopes=()
        )
    try:
        payload: Any = json.loads(result.stdout)
        hosts = payload.get("hosts", payload)
        active = next(
            account
            for accounts in hosts.values()
            for account in accounts
            if account.get("active")
        )
        raw_scopes = str(active.get("scopes") or "")
        scopes = tuple(sorted(item.strip() for item in raw_scopes.split(",") if item.strip()))
    except (KeyError, TypeError, ValueError, StopIteration, AttributeError):
        return TokenDiagnosis(
            authenticated=False, scopes=(), can_discover=False, can_write=False, broad_scopes=()
        )
    broad = tuple(scope for scope in scopes if scope in WRITE_CAPABLE_SCOPES)
    return TokenDiagnosis(
        authenticated=True,
        scopes=scopes,
        can_discover=True,
        can_write=bool(broad),
        broad_scopes=broad,
    )


def _github_environment() -> dict[str, str]:
    allowed = {
        "ALL_PROXY",
        "all_proxy",
        "CURL_CA_BUNDLE",
        "GH_CONFIG_DIR",
        "GH_ENTERPRISE_TOKEN",
        "GH_HOST",
        "GH_HTTP_UNIX_SOCKET",
        "GH_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_TOKEN",
        "HOME",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "NO_PROXY",
        "no_proxy",
        "PATH",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "XDG_CONFIG_HOME",
    }
    return {name: value for name, value in os.environ.items() if name in allowed}


class GitHubIssueSource:
    def __init__(self, run: RunCommand | None = None) -> None:
        self._run = run

    def list_open(
        self, repo: str, *, label: str | None = None, limit: int = 1000
    ) -> tuple[Issue, ...]:
        validate_repo(repo)
        if limit <= 0:
            raise ValueError("limit must be positive")
        argv = [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(limit),
        ]
        if label:
            argv.extend(("--label", label))
        argv.extend(("--json", "number,title,body,labels,url"))
        try:
            runner = self._run or subprocess.run
            result = runner(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=_github_environment(),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise GitHubCliError("GitHub CLI could not be executed") from error
        if result.returncode != 0:
            raise GitHubCliError(f"GitHub CLI exited with {result.returncode}; stderr hidden")
        try:
            payload: Any = json.loads(result.stdout)
            if not isinstance(payload, list):
                raise TypeError
            return tuple(self._parse_issue(value) for value in payload)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise GitHubCliError("GitHub CLI returned an invalid issue payload") from error

    @staticmethod
    def _parse_issue(value: Any) -> Issue:
        if not isinstance(value, dict):
            raise TypeError("issue must be a dict")
        raw_labels = value.get("labels", [])
        if not isinstance(raw_labels, list):
            raw_labels = []
        parsed_labels: list[str] = []
        for item in raw_labels:
            if isinstance(item, dict):
                name = item.get("name")
                if name is not None:
                    parsed_labels.append(str(name))
            elif isinstance(item, str) and item:
                parsed_labels.append(item)
        return Issue(
            number=int(value["number"]),
            title=str(value["title"]),
            body=str(value.get("body") or ""),
            labels=tuple(parsed_labels),
            url=str(value["url"]) if value.get("url") else None,
        )
