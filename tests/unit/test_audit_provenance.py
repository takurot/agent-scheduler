from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from subsched.audit_inventory import canonical_inventory, expected_cache_paths
from subsched.audit_provenance import (
    AuditPolicy,
    authenticate_audit_artifact,
    authenticate_cache_bundle,
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fixture() -> tuple[AuditPolicy, dict, dict, bytes, bytes, datetime]:
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    workflow = b"trusted workflow definition\n"
    target = {
        "repository": "owner/repo",
        "commit": "a" * 40,
        "lock_sha256": "b" * 64,
        "packages_sha256": "c" * 64,
        "tool": "pip-audit 2.10.0",
        "policy": "python-prod-dev-v1",
        "workflow_sha256": sha(workflow),
    }
    policy = AuditPolicy(
        repository_id=11,
        workflow_id=22,
        workflow_commit="d" * 40,
        target=target,
        max_age=timedelta(hours=24),
    )
    run = {
        "id": 33,
        "run_attempt": 1,
        "run_started_at": (now - timedelta(minutes=2)).isoformat(),
        "workflow_id": 22,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_sha": "d" * 40,
        "repository": {"id": 11},
        "head_repository": {"id": 11},
    }
    payload = json.dumps(
        {
            "schema": 1,
            "target": target,
            "observed_at": (now - timedelta(minutes=1)).isoformat(),
            "outcome": "clean",
        }
    ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("audit.json", payload)
    data = buffer.getvalue()
    artifact = {
        "id": 44,
        "name": "audit-evidence-1",
        "expired": False,
        "created_at": (now - timedelta(minutes=1)).isoformat(),
        "digest": "sha256:" + sha(data),
        "workflow_run": {
            "id": 33,
            "repository_id": 11,
            "head_repository_id": 11,
            "head_sha": "d" * 40,
        },
    }
    return policy, run, artifact, workflow, data, now


def test_accepts_only_trusted_fresh_clean_artifact() -> None:
    policy, run, artifact, workflow, data, now = fixture()
    assert authenticate_audit_artifact(policy, run, artifact, workflow, data, now) == "clean"


@pytest.mark.parametrize(
    "case",
    [
        "fork",
        "workflow",
        "commit",
        "event",
        "running",
        "failed",
        "run",
        "artifact_repo",
        "digest",
        "expired",
        "definition",
        "old_attempt",
        "artifact_attempt",
    ],
)
def test_rejects_untrusted_ci_provenance(case: str) -> None:
    policy, run, artifact, workflow, data, now = fixture()
    if case == "fork":
        run["head_repository"]["id"] = 12
    elif case == "workflow":
        run["workflow_id"] = 23
    elif case == "commit":
        run["head_sha"] = "e" * 40
    elif case == "event":
        run["event"] = "pull_request"
    elif case == "running":
        run["status"] = "in_progress"
    elif case == "failed":
        run["conclusion"] = "failure"
    elif case == "run":
        artifact["workflow_run"]["id"] = 34
    elif case == "artifact_repo":
        artifact["workflow_run"]["repository_id"] = 12
    elif case == "digest":
        data += b"tampered"
    elif case == "expired":
        artifact["expired"] = True
    elif case == "old_attempt":
        artifact["created_at"] = (now - timedelta(hours=1)).isoformat()
    elif case == "artifact_attempt":
        artifact["name"] = "audit-evidence-2"
    else:
        workflow += b"changed"
    assert authenticate_audit_artifact(policy, run, artifact, workflow, data, now) != "clean"


def test_rejects_unexpected_zip_members() -> None:
    policy, run, artifact, workflow, _, now = fixture()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../audit.json", b"{}")
    data = buffer.getvalue()
    artifact["digest"] = "sha256:" + sha(data)
    assert authenticate_audit_artifact(policy, run, artifact, workflow, data, now) != "clean"


@pytest.mark.parametrize(
    "case", ["advisory", "unavailable", "target", "stale", "future", "duplicate"]
)
def test_rejects_unacceptable_evidence(case: str) -> None:
    policy, run, artifact, workflow, data, now = fixture()
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        record = json.loads(zipped.read("audit.json"))
    if case in {"advisory", "unavailable"}:
        record["outcome"] = case
    elif case == "target":
        record["target"]["lock_sha256"] = "f" * 64
    elif case == "stale":
        record["observed_at"] = (now - timedelta(hours=24)).isoformat()
        run["run_started_at"] = (now - timedelta(hours=25)).isoformat()
        artifact["created_at"] = (now - timedelta(hours=23)).isoformat()
    elif case == "future":
        record["observed_at"] = (now + timedelta(seconds=1)).isoformat()
    payload = json.dumps(record).encode()
    if case == "duplicate":
        payload = payload[:-1] + b', "outcome":"clean"}'
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("audit.json", payload)
    data = buffer.getvalue()
    artifact["digest"] = "sha256:" + sha(data)
    assert authenticate_audit_artifact(policy, run, artifact, workflow, data, now) != "clean"


def test_malformed_nested_api_response_fails_closed() -> None:
    policy, run, artifact, workflow, data, now = fixture()
    run["head_repository"] = "not an object"
    assert authenticate_audit_artifact(policy, run, artifact, workflow, data, now) != "clean"


@pytest.mark.parametrize(
    "problem", ["none", "missing", "extra", "traversal", "inventory", "tampered"]
)
def test_authenticated_bundle_requires_exact_inventory_and_cache(problem: str) -> None:
    policy, run, artifact, workflow, _, now = fixture()
    items = [("packaging", "26.3")]
    inventory = canonical_inventory(items)
    target = {**policy.target, "packages_sha256": sha(inventory)}
    policy = AuditPolicy(
        policy.repository_id, policy.workflow_id, policy.workflow_commit, target, policy.max_age
    )
    record = {
        "schema": 1,
        "target": target,
        "observed_at": (now - timedelta(minutes=1)).isoformat(),
        "outcome": "clean",
    }
    path = next(iter(expected_cache_paths(items)))
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("audit.json", json.dumps(record))
        zipped.writestr("inventory.json", b"[]" if problem == "inventory" else inventory)
        if problem != "missing":
            zipped.writestr("cache/" + path, b"trusted-cache")
        if problem == "extra":
            zipped.writestr("cache/extra", b"x")
        if problem == "traversal":
            zipped.writestr("cache/../escape", b"x")
    data = buffer.getvalue()
    artifact["digest"] = "sha256:" + sha(data)
    if problem == "tampered":
        data += b"x"
    cache = authenticate_cache_bundle(policy, run, artifact, workflow, data, now, items)
    assert cache == ({path: b"trusted-cache"} if problem == "none" else None)


def test_corrupt_compressed_artifact_fails_closed() -> None:
    policy, run, artifact, workflow, data, now = fixture()
    corrupted = bytearray(data)
    with zipfile.ZipFile(io.BytesIO(data)) as zipped:
        member = zipped.getinfo("audit.json")
        offset = member.header_offset + 30 + len(member.filename.encode()) + len(member.extra)
    corrupted[offset:offset + member.compress_size] = b'\xff' * member.compress_size
    data = bytes(corrupted)
    artifact["digest"] = "sha256:" + sha(data)
    assert authenticate_audit_artifact(policy, run, artifact, workflow, data, now) == "invalid"
