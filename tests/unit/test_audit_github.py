from __future__ import annotations

import base64
import hashlib
import io
import json
import zipfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_audit_provenance import fixture

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.audit_github import (
    AuditTransportIsolationError,
    fetch_github_audit,
    fetch_github_cache,
)
from subsched.audit_inventory import canonical_inventory, expected_cache_paths


@pytest.mark.parametrize(
    "problem",
    [
        "none",
        "unrelated_metadata",
        "race",
        "wrong_run",
        "wrong_artifact",
        "transport",
        "command_not_found",
        "workflow_path",
    ],
)
def test_fetch_verifies_authenticated_responses_and_rechecks_run(problem: str) -> None:
    policy, run, artifact, workflow, archive, now = fixture()
    calls: list[ProcessExecutionRequest] = []
    latest_reads = 0

    def fake_runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        nonlocal latest_reads
        calls.append(request)
        command = request.argv
        if problem == "transport":
            return ProcessExecutionResult(exit_code=1, stdout="", stderr="secret must stay hidden")
        if command[0] == "/bin/bash":
            return ProcessExecutionResult(exit_code=0, stdout=base64.b64encode(archive).decode())
        endpoint = command[-1]
        if "/contents/" in endpoint:
            data = {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(workflow).decode(),
            }
        elif "/actions/workflows/" in endpoint:
            data = {
                "id": 22,
                "path": ".github/workflows/other.yml"
                if problem == "workflow_path"
                else ".github/workflows/independent-audit.yml",
                "state": "active",
            }
        elif "/actions/artifacts/" in endpoint:
            data = {**artifact, "id": 45 if problem == "wrong_artifact" else 44}
        else:
            data = dict(run)
            if "/attempts/" not in endpoint:
                latest_reads += 1
                if problem == "race" and latest_reads == 2:
                    data["run_attempt"] = 2
                if problem == "unrelated_metadata" and latest_reads == 2:
                    data["repository"] = {**data["repository"], "description": "updated"}
            if problem == "wrong_run":
                data["id"] = 34
        return ProcessExecutionResult(
            exit_code=0, stdout=json.dumps(data), command_not_found=problem == "command_not_found"
        )

    outcome = fetch_github_audit(
        "owner/repo", 33, 44, policy, now, gh=Path("/usr/bin/true"), runner=fake_runner
    )
    assert outcome == ("clean" if problem in {"none", "unrelated_metadata"} else "unavailable")
    if problem in {"none", "unrelated_metadata"}:
        assert latest_reads == 2
        assert all("secret must stay hidden" not in " ".join(call.argv) for call in calls)
        assert all(call.output_limit_bytes <= 2 * 1024 * 1024 for call in calls)


def test_fetch_cache_requires_ci_authentication_and_complete_scope() -> None:
    policy, run, artifact, workflow, _, now = fixture()
    items = [("packaging", "26.3")]
    inventory = canonical_inventory(items)
    target = {**policy.target, "packages_sha256": hashlib.sha256(inventory).hexdigest()}
    policy = replace(policy, target=target)
    record = {
        "schema": 1,
        "target": target,
        "observed_at": (now - timedelta(minutes=1)).isoformat(),
        "outcome": "clean",
    }
    name = next(iter(expected_cache_paths(items)))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("audit.json", json.dumps(record))
        zipped.writestr("inventory.json", inventory)
        zipped.writestr("cache/" + name, b"cache-data")
    archive = buffer.getvalue()
    artifact["digest"] = "sha256:" + hashlib.sha256(archive).hexdigest()

    def fake_runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        if request.argv[0] == "/bin/bash":
            return ProcessExecutionResult(exit_code=0, stdout=base64.b64encode(archive).decode())
        endpoint = request.argv[-1]
        if "/contents/" in endpoint:
            response = {
                "type": "file",
                "encoding": "base64",
                "content": base64.b64encode(workflow).decode(),
            }
        elif "/actions/workflows/" in endpoint:
            response = {
                "id": 22,
                "path": ".github/workflows/independent-audit.yml",
                "state": "active",
            }
        elif "/actions/artifacts/" in endpoint:
            response = artifact
        else:
            response = run
        return ProcessExecutionResult(exit_code=0, stdout=json.dumps(response))

    assert fetch_github_cache(
        "owner/repo", 33, 44, policy, now, items, gh=Path("/usr/bin/true"), runner=fake_runner
    ) == {name: b"cache-data"}
    assert (
        fetch_github_cache(
            "owner/repo",
            33,
            44,
            policy,
            now,
            [("other", "1")],
            gh=Path("/usr/bin/true"),
            runner=fake_runner,
        )
        is None
    )


def test_unconfirmed_github_process_cleanup_is_not_an_ordinary_unavailable_result() -> None:
    policy, _, _, _, _, now = fixture()

    def runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        return ProcessExecutionResult(exit_code=1, stdout="", cleanup_succeeded=False)

    with pytest.raises(AuditTransportIsolationError):
        fetch_github_cache(
            "owner/repo",
            33,
            44,
            policy,
            now,
            [("demo", "1")],
            gh=Path("/usr/bin/true"),
            runner=runner,
        )
