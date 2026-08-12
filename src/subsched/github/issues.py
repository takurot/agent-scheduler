from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from typing import Any

from subsched.config import validate_repo
from subsched.models import Issue


class GitHubCliError(RuntimeError):
    pass


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def _github_environment() -> dict[str, str]:
    allowed = {
        "GH_CONFIG_DIR",
        "GH_ENTERPRISE_TOKEN",
        "GH_HOST",
        "GH_TOKEN",
        "GITHUB_ENTERPRISE_TOKEN",
        "GITHUB_TOKEN",
        "HOME",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
        "PATH",
        "XDG_CONFIG_HOME",
    }
    return {name: value for name, value in os.environ.items() if name in allowed}


class GitHubIssueSource:
    def __init__(self, run: RunCommand | None = None) -> None:
        self._run = run

    def list_open(self, repo: str, *, label: str | None = None) -> tuple[Issue, ...]:
        validate_repo(repo)
        argv = [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            "100",
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
        if not isinstance(value, dict) or not isinstance(value.get("labels", []), list):
            raise TypeError
        labels = tuple(str(label["name"]) for label in value.get("labels", []))
        return Issue(
            number=int(value["number"]),
            title=str(value["title"]),
            body=str(value.get("body") or ""),
            labels=labels,
            url=str(value["url"]) if value.get("url") else None,
        )
