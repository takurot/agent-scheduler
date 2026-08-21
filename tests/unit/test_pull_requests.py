from __future__ import annotations

import json
import subprocess

import pytest

from subsched.github.pull_requests import (
    build_pr_body,
    create_or_get_pull_request,
    lookup_existing_pr,
)
from subsched.models import Issue, Task


def test_build_pr_body_avoids_fixes_or_closes() -> None:
    body = build_pr_body(103, summary="Add timeout logic", verification_results="pytest: PASS")
    assert "Implements work for #103" in body
    assert "Fixes #" not in body
    assert "Closes #" not in body
    assert "Issue is intentionally left open until review." in body


def test_lookup_existing_pr_found(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_json = json.dumps([
        {
            "number": 66,
            "url": "https://github.com/takurot/agent-scheduler/pull/66",
            "title": "Verification runner (#26)",
            "body": "Implements work for #26",
        }
    ])
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(["gh"], 0, stdout=fake_json, stderr=""),
    )
    pr = lookup_existing_pr("issue/26-verification-runner")
    assert pr is not None
    assert pr.number == 66
    assert "/pull/66" in pr.url


def test_create_or_get_pull_request_creates_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if "list" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="[]", stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout="https://github.com/takurot/agent-scheduler/pull/68\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    task = Task.from_issue(Issue(number=103, title="Support timeout"))
    pr = create_or_get_pull_request(task, "issue/103-timeout")
    assert pr is not None
    assert pr.number == 68
    assert len(calls) == 2
