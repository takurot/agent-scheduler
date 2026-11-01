from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.audit_inventory import canonical_inventory, produce_audit_record
from subsched.audit_provenance import AuditPolicy, authenticate_audit_artifact


def test_producer_and_authenticated_consumer_bind_exact_inventory(tmp_path: Path) -> None:
    now = datetime(2026, 11, 1, 12, tzinfo=UTC)
    workflow = b"trusted workflow\n"

    def sha(value: bytes) -> str:
        return hashlib.sha256(value).hexdigest()

    inventory = [("alpha", "1"), ("beta", "2")]
    target = {
        "repository": "owner/repo",
        "commit": "a" * 40,
        "lock_sha256": "b" * 64,
        "packages_sha256": sha(canonical_inventory(inventory)),
        "tool": "pip-audit 2.10.1",
        "policy": "scope-v1",
        "workflow_sha256": sha(workflow),
    }
    tool = tmp_path / "pip-audit"
    tool.touch()

    def runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        if request.argv[-1] == "--version":
            return ProcessExecutionResult(exit_code=0, stdout="pip-audit 2.10.1\n")
        return ProcessExecutionResult(
            exit_code=0,
            stdout=json.dumps(
                {
                    "dependencies": [
                        {"name": "alpha", "version": "1", "vulns": []},
                        {"name": "beta", "version": "2", "vulns": []},
                    ],
                    "fixes": [],
                }
            ),
        )

    record = produce_audit_record(inventory, target, tool, now, runner=runner)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr("audit.json", json.dumps(record))
    archive = buffer.getvalue()
    run = {
        "id": 33,
        "run_attempt": 1,
        "run_started_at": (now - timedelta(seconds=1)).isoformat(),
        "workflow_id": 22,
        "event": "workflow_dispatch",
        "status": "completed",
        "conclusion": "success",
        "head_sha": "d" * 40,
        "repository": {"id": 11},
        "head_repository": {"id": 11},
    }
    artifact = {
        "id": 44,
        "name": "audit-evidence-1",
        "expired": False,
        "created_at": now.isoformat(),
        "digest": "sha256:" + sha(archive),
        "workflow_run": {
            "id": 33,
            "repository_id": 11,
            "head_repository_id": 11,
            "head_sha": "d" * 40,
        },
    }
    policy = AuditPolicy(11, 22, "d" * 40, target, timedelta(hours=24))
    assert authenticate_audit_artifact(policy, run, artifact, workflow, archive, now) == "clean"
    changed = {**target, "packages_sha256": sha(canonical_inventory([("alpha", "1")]))}
    other_policy = AuditPolicy(11, 22, "d" * 40, changed, timedelta(hours=24))
    assert (
        authenticate_audit_artifact(other_policy, run, artifact, workflow, archive, now)
        == "target_mismatch"
    )
