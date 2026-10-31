"""Synthetic evidence only: these fixtures do not attest a real online audit."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.audit_contract_spike import evaluate_audit_evidence

NOW = datetime(2026, 10, 30, 16, tzinfo=UTC)
TARGET = {
    "repository": "owner/repository",
    "commit": "a" * 40,
    "lock_sha256": "b" * 64,
    "packages_sha256": "c" * 64,
    "tool": "pip-audit==2.10.0",
    "policy": "pypi/no-ignores/v1",
    "workflow_sha256": "d" * 64,
}


def evidence(**updates: object) -> bytes:
    record = {
        "schema": 1,
        "target": TARGET,
        "observed_at": NOW.isoformat(),
        "outcome": "clean",
    }
    record.update(updates)
    return json.dumps(record, sort_keys=True).encode()


def evaluate(payload: bytes | None, *, digest: str | None = None) -> str:
    return evaluate_audit_evidence(
        payload,
        authenticated_sha256=digest or (hashlib.sha256(payload).hexdigest() if payload else None),
        expected_target=TARGET,
        now=NOW,
    )


def test_clean_evidence() -> None:
    assert evaluate(evidence()) == "clean"


def test_advisory_is_not_clean() -> None:
    assert evaluate(evidence(outcome="advisory")) == "advisory"


def test_missing_evidence() -> None:
    assert evaluate(None) == "missing"


def test_unavailable_audit_is_not_clean() -> None:
    assert evaluate(evidence(outcome="unavailable")) == "unavailable"


@pytest.mark.parametrize("age", [timedelta(hours=24), timedelta(days=2), -timedelta(seconds=1)])
def test_stale_or_future_metadata(age: timedelta) -> None:
    assert evaluate(evidence(observed_at=(NOW - age).isoformat())) == "stale"


@pytest.mark.parametrize("field", list(TARGET))
def test_different_target(field: str) -> None:
    replacement = (
        "e" * len(TARGET[field])
        if field.endswith("sha256") or field == "commit"
        else "different"
    )
    assert evaluate(evidence(target={**TARGET, field: replacement})) == "target_mismatch"


def test_tampered_evidence() -> None:
    trusted_digest = hashlib.sha256(evidence()).hexdigest()
    assert evaluate(evidence(outcome="advisory"), digest=trusted_digest) == "untrusted"


def test_worker_evidence_without_authenticated_digest() -> None:
    assert evaluate_audit_evidence(
        evidence(), authenticated_sha256=None, expected_target=TARGET, now=NOW
    ) == "untrusted"


@pytest.mark.parametrize("payload", [b"not json", b"[]", b"null", b'{"schema":true}'])
def test_malformed_evidence(payload: bytes) -> None:
    assert evaluate(payload) == "invalid"


@pytest.mark.parametrize("updates", [
    {"schema": 2}, {"outcome": "skipped"}, {"extra": True},
    {"observed_at": "2026-10-30T16:00:00"}, {"observed_at": "invalid"},
    {"target": {**TARGET, "lock_sha256": "invalid"}},
    {"target": {**TARGET, "extra": "unknown"}},
])
def test_invalid_schema(updates: dict[str, object]) -> None:
    assert evaluate(evidence(**updates)) == "invalid"


def test_duplicate_keys_rejected() -> None:
    payload = evidence().replace(b'"schema": 1', b'"schema": 2, "schema": 1')
    assert evaluate(payload) == "invalid"


def test_unknown_expected_target_fails_closed() -> None:
    with pytest.raises(ValueError, match="target"):
        evaluate_audit_evidence(
            evidence(), authenticated_sha256=None, expected_target={}, now=NOW
        )


def test_naive_clock_fails_closed() -> None:
    with pytest.raises(ValueError, match="clock"):
        evaluate_audit_evidence(
            evidence(), authenticated_sha256=None, expected_target=TARGET,
            now=NOW.replace(tzinfo=None),
        )
