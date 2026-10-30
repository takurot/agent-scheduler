"""Issue #495 contract spike; NOT a Scheduler gate or an evidence authenticator.

The caller must obtain authenticated_sha256 from independently trusted CI control
plane data, never from the worker, repository, or an adjacent checksum file.
No I/O or execution of evidence-derived values occurs here.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta

_TARGET_KEYS = {
    "repository", "commit", "lock_sha256", "packages_sha256", "tool", "policy",
    "workflow_sha256",
}
MAX_AGE = timedelta(hours=24)


def _valid_target(target: object) -> bool:
    if not isinstance(target, dict) or target.keys() != _TARGET_KEYS:
        return False
    if not all(isinstance(value, str) and value.strip() for value in target.values()):
        return False
    return all(
        re.fullmatch(r"[0-9a-f]{40}", value) is not None if key == "commit"
        else re.fullmatch(r"[0-9a-f]{64}", value) is not None if key.endswith("sha256")
        else True
        for key, value in target.items()
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def evaluate_audit_evidence(
    payload: bytes | None,
    *,
    authenticated_sha256: str | None,
    expected_target: dict[str, str],
    now: datetime,
) -> str:
    """Classify a bounded CI record; only 'clean' satisfies the audit contract.

    This prototype cannot establish provenance. Authentication, immutable run ID
    and attempt, CI origin, and trusted workflow checks belong to the caller.
    """
    if not _valid_target(expected_target):
        raise ValueError("unknown expected target")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware clock required")
    if payload is None:
        return "missing"
    if (
        not isinstance(authenticated_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", authenticated_sha256) is None
        or hashlib.sha256(payload).hexdigest() != authenticated_sha256
    ):
        return "untrusted"
    if len(payload) > 65536:
        return "invalid"
    try:
        record = json.loads(payload, object_pairs_hook=_unique_object)
        if (
            not isinstance(record, dict)
            or record.keys() != {"schema", "target", "observed_at", "outcome"}
            or type(record["schema"]) is not int
            or record["schema"] != 1
            or not _valid_target(record["target"])
            or record["outcome"] not in ("clean", "advisory", "unavailable")
            or not isinstance(record["observed_at"], str)
        ):
            return "invalid"
        observed_at = datetime.fromisoformat(record["observed_at"])
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            return "invalid"
    except (ValueError, UnicodeError, RecursionError):
        return "invalid"
    if record["target"] != expected_target:
        return "target_mismatch"
    if not timedelta(0) <= now - observed_at < MAX_AGE:
        return "stale"
    return str(record["outcome"])
