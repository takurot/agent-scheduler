"""Validate audit artifacts after their metadata is obtained from the trusted CI API.

This module does not fetch data, authenticate a network connection, or change a
Scheduler gate. Callers must independently authenticate every API response.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
import zlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from subsched.audit_inventory import canonical_inventory, expected_cache_paths

_TARGET_KEYS = {
    "repository",
    "commit",
    "lock_sha256",
    "packages_sha256",
    "tool",
    "policy",
    "workflow_sha256",
}
_MAX_ARCHIVE = 1024 * 1024
_MAX_RECORD = 65536
_MAX_UNPACKED = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AuditPolicy:
    """Operator-controlled expected identity; never derive this from an artifact."""

    repository_id: int
    workflow_id: int
    workflow_commit: str
    target: dict[str, str]
    max_age: timedelta


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return timestamp


def _safe_zip_members(zipped: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members = zipped.infolist()
    if not members or len(members) > 10002:
        raise ValueError("invalid artifact entry count")
    result: dict[str, zipfile.ZipInfo] = {}
    total = 0
    for member in members:
        name = member.filename
        mode = member.external_attr >> 16
        if (
            name in result
            or member.is_dir()
            or member.flag_bits & 1
            or member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
            or not (
                name in {"audit.json", "inventory.json"}
                or re.fullmatch(r"cache/(?:[0-9a-f]/){5}[0-9a-f]{56}", name)
            )
            or member.file_size > 1024 * 1024
        ):
            raise ValueError("invalid artifact entry")
        total += member.file_size
        if total > _MAX_UNPACKED:
            raise ValueError("artifact too large")
        result[name] = member
    return result


def authenticate_audit_artifact(
    policy: AuditPolicy,
    run: dict[str, Any],
    artifact: dict[str, Any],
    workflow: bytes,
    archive: bytes,
    now: datetime,
) -> str:
    """Classify an audit record; this alone cannot satisfy an offline gate.

    The run, artifact, and workflow bytes must come from authenticated GitHub API
    responses. A local file or worker-provided metadata is not authenticated.
    A networkless gate also requires the exact cache via authenticate_cache_bundle.
    """
    try:
        target = policy.target
        if (
            type(policy.repository_id) is not int
            or policy.repository_id <= 0
            or type(policy.workflow_id) is not int
            or policy.workflow_id <= 0
            or not re.fullmatch(r"[0-9a-f]{40}", policy.workflow_commit)
            or policy.max_age <= timedelta(0)
            or policy.max_age > timedelta(hours=24)
            or now.tzinfo is None
            or now.utcoffset() is None
            or target.keys() != _TARGET_KEYS
            or not all(isinstance(value, str) and value.strip() for value in target.values())
            or not re.fullmatch(r"[0-9a-f]{40}", target["commit"])
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", target[key])
                for key in ("lock_sha256", "packages_sha256", "workflow_sha256")
            )
        ):
            return "invalid_policy"
        if (
            hashlib.sha256(workflow).hexdigest() != target["workflow_sha256"]
            or run.get("workflow_id") != policy.workflow_id
            or run.get("event") != "workflow_dispatch"
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or run.get("head_sha") != policy.workflow_commit
            or run.get("repository", {}).get("id") != policy.repository_id
            or run.get("head_repository", {}).get("id") != policy.repository_id
            or type(run.get("id")) is not int
            or run["id"] <= 0
            or type(run.get("run_attempt")) is not int
            or run["run_attempt"] <= 0
            or artifact.get("name") != f"audit-evidence-{run['run_attempt']}"
            or artifact.get("expired") is not False
            or type(artifact.get("id")) is not int
            or artifact["id"] <= 0
            or artifact.get("workflow_run", {}).get("id") != run["id"]
            or artifact.get("workflow_run", {}).get("repository_id") != policy.repository_id
            or artifact.get("workflow_run", {}).get("head_repository_id") != policy.repository_id
            or artifact.get("workflow_run", {}).get("head_sha") != policy.workflow_commit
            or not _time(run.get("run_started_at")) <= _time(artifact.get("created_at")) <= now
            or len(archive) > _MAX_ARCHIVE
            or artifact.get("digest") != "sha256:" + hashlib.sha256(archive).hexdigest()
        ):
            return "untrusted"
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            members = _safe_zip_members(zipped)
            if "audit.json" not in members or members["audit.json"].file_size > _MAX_RECORD:
                return "invalid"
            payload = zipped.read(members["audit.json"])
        record = json.loads(payload, object_pairs_hook=_unique_object)
        if (
            not isinstance(record, dict)
            or record.keys() != {"schema", "target", "observed_at", "outcome"}
            or type(record["schema"]) is not int
            or record["schema"] != 1
            or record["outcome"] not in ("clean", "advisory", "unavailable")
        ):
            return "invalid"
        if record["target"] != target:
            return "target_mismatch"
        observed = _time(record["observed_at"])
        if not _time(run["run_started_at"]) <= observed <= _time(artifact["created_at"]):
            return "untrusted"
        if not timedelta(0) <= now - observed < policy.max_age:
            return "stale"
        return str(record["outcome"])
    except (
        AttributeError,
        KeyError,
        TypeError,
        ValueError,
        UnicodeError,
        RecursionError,
        zipfile.BadZipFile,
        RuntimeError,
        OverflowError,
        EOFError,
        zlib.error,
    ):
        return "invalid"


def authenticate_cache_bundle(
    policy: AuditPolicy,
    run: dict[str, Any],
    artifact: dict[str, Any],
    workflow: bytes,
    archive: bytes,
    now: datetime,
    inventory: Sequence[tuple[str, str]],
) -> dict[str, bytes] | None:
    """Return the complete cache only after CI provenance and exact scope pass."""
    if authenticate_audit_artifact(policy, run, artifact, workflow, archive, now) != "clean":
        return None
    try:
        expected_inventory = canonical_inventory(inventory)
        if hashlib.sha256(expected_inventory).hexdigest() != policy.target["packages_sha256"]:
            return None
        paths = expected_cache_paths(inventory)
        expected = {"audit.json", "inventory.json", *("cache/" + path for path in paths)}
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            members = _safe_zip_members(zipped)
            if set(members) != expected or members["inventory.json"].file_size > _MAX_RECORD:
                return None
            if zipped.read(members["inventory.json"]) != expected_inventory:
                return None
            cache = {path: zipped.read(members["cache/" + path]) for path in paths}
            if any(not content for content in cache.values()):
                return None
            return cache
    except (KeyError, TypeError, ValueError, OSError, zipfile.BadZipFile, RuntimeError,
            EOFError, zlib.error):
        return None
