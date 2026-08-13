from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class AssumptionManifestError(ValueError):
    pass


class AssumptionDecision(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


SENSITIVE_FLAGS = frozenset(
    {
        "--api-key",
        "--access-token",
        "--auth-token",
        "--client-secret",
        "--credential",
        "--password",
        "--print",
        "--prompt",
        "--prompt-file",
        "--token",
        "-p",
    }
)
ALLOWED_LIVE_PROBES = frozenset(
    {
        ("claude", "--version"),
        ("codex", "--version"),
        ("gh", "--version"),
        ("gh", "version"),
    }
)
REDACTED = "[REDACTED]"
REDACTED_PATH = "[REDACTED_PATH]"
SCHEMA_VERSION = 1
RECORD_KEYS = frozenset(
    {
        "cli_name",
        "cli_version",
        "argv",
        "timestamp",
        "exit_code",
        "schema_hash",
        "decision",
        "verified",
    }
)
MANIFEST_KEYS = frozenset({"schema_version", "date", "records"})
HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
CLI_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,63}")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
SECRET_PATTERN = re.compile(
    r"(?i)(?:github_pat_[a-z0-9_]+|gh[pousr]_[a-z0-9_]{10,}|sk-[a-z0-9_-]{20,}|"
    r"xox[baprs]-[a-z0-9-]{10,}|bearer\s+[a-z0-9._~-]+)"
)


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class AssumptionRecord:
    cli_name: str
    cli_version: str
    argv: tuple[str, ...]
    timestamp: datetime
    exit_code: int
    schema_hash: str
    decision: AssumptionDecision
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "cli_name": self.cli_name,
            "cli_version": self.cli_version,
            "argv": list(self.argv),
            "timestamp": self.timestamp.isoformat(),
            "exit_code": self.exit_code,
            "schema_hash": self.schema_hash,
            "decision": self.decision.value,
            "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class AssumptionManifest:
    date: str
    records: tuple[AssumptionRecord, ...]
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "date": self.date,
            "records": [record.to_dict() for record in self.records],
        }


@dataclass(frozen=True, slots=True)
class LiveProbeResult:
    returncode: int
    stdout: str
    stderr: str


def _personal_roots(roots: Sequence[Path] | None) -> tuple[Path, ...]:
    selected = tuple(roots) if roots is not None else (Path.home(),)
    return tuple(root.expanduser().resolve(strict=False) for root in selected)


def _redact_argument(value: str, roots: tuple[Path, ...]) -> str:
    if SECRET_PATTERN.search(value):
        return REDACTED
    if any(str(root) in value for root in roots):
        return REDACTED_PATH
    return value


def redact_argv(
    argv: Sequence[str], *, personal_roots: Sequence[Path] | None = None
) -> tuple[str, ...]:
    roots = _personal_roots(personal_roots)
    redacted: list[str] = []
    redact_next = False
    redact_codex_positionals = len(argv) >= 2 and tuple(argv[0:2]) == ("codex", "exec")
    for index, value in enumerate(argv):
        if not isinstance(value, str):
            raise AssumptionManifestError("argv entries must be strings")
        if redact_next:
            redacted.append(REDACTED)
            redact_next = False
            continue
        flag, separator, _ = value.partition("=")
        if flag.casefold() in SENSITIVE_FLAGS:
            if separator:
                redacted.append(f"{flag}={REDACTED}")
            else:
                redacted.append(value)
                redact_next = True
            continue
        if redact_codex_positionals and index >= 2 and not value.startswith("-"):
            redacted.append(REDACTED)
            continue
        redacted.append(_redact_argument(value, roots))
    if redact_next:
        raise AssumptionManifestError("sensitive argv flag is missing its value")
    return tuple(redacted)


def create_record(
    *,
    cli_name: str,
    cli_version: str,
    argv: Sequence[str],
    timestamp: datetime,
    exit_code: int,
    schema_hash: str,
    decision: AssumptionDecision,
    verified: bool = False,
    personal_roots: Sequence[Path] | None = None,
) -> AssumptionRecord:
    if not isinstance(cli_name, str) or not CLI_PATTERN.fullmatch(cli_name):
        raise AssumptionManifestError("invalid cli_name")
    if not isinstance(cli_version, str) or not cli_version.strip() or len(cli_version) > 256:
        raise AssumptionManifestError("invalid cli_version")
    if not isinstance(argv, (list, tuple)) or not argv:
        raise AssumptionManifestError("argv must not be empty")
    if (
        not isinstance(timestamp, datetime)
        or timestamp.tzinfo is None
        or timestamp.utcoffset() is None
    ):
        raise AssumptionManifestError("timestamp must include a timezone")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        raise AssumptionManifestError("exit_code must be an integer")
    if not isinstance(schema_hash, str) or not HASH_PATTERN.fullmatch(schema_hash):
        raise AssumptionManifestError("schema_hash must be a lowercase SHA-256 digest")
    if not isinstance(decision, AssumptionDecision):
        raise AssumptionManifestError("invalid decision")
    if not isinstance(verified, bool):
        raise AssumptionManifestError("verified must be a boolean")
    if decision is AssumptionDecision.PASS and not verified:
        raise AssumptionManifestError("PASS requires an explicitly verified record")
    return AssumptionRecord(
        cli_name=cli_name,
        cli_version=cli_version,
        argv=redact_argv(argv, personal_roots=personal_roots),
        timestamp=timestamp,
        exit_code=exit_code,
        schema_hash=schema_hash,
        decision=decision,
        verified=verified,
    )


def _require_exact_keys(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    if set(value) != expected:
        raise AssumptionManifestError(f"invalid {label} fields")


def _parse_record(value: Any) -> AssumptionRecord:
    if not isinstance(value, dict):
        raise AssumptionManifestError("record must be an object")
    _require_exact_keys(value, RECORD_KEYS, "record")
    argv = value["argv"]
    if not isinstance(argv, list):
        raise AssumptionManifestError("argv must be an array")
    try:
        timestamp = datetime.fromisoformat(value["timestamp"])
        decision = AssumptionDecision(value["decision"])
    except (TypeError, ValueError) as error:
        raise AssumptionManifestError("invalid record value") from error
    record = create_record(
        cli_name=value["cli_name"],
        cli_version=value["cli_version"],
        argv=argv,
        timestamp=timestamp,
        exit_code=value["exit_code"],
        schema_hash=value["schema_hash"],
        decision=decision,
        verified=value["verified"],
    )
    if tuple(argv) != record.argv:
        raise AssumptionManifestError("manifest contains unredacted argv")
    return record


def load_manifest(path: Path) -> AssumptionManifest:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AssumptionManifestError("cannot read assumption manifest") from error
    if not isinstance(value, dict):
        raise AssumptionManifestError("manifest must be an object")
    _require_exact_keys(value, MANIFEST_KEYS, "manifest")
    if value["schema_version"] != SCHEMA_VERSION:
        raise AssumptionManifestError("unsupported schema_version")
    date = value["date"]
    records = value["records"]
    if not isinstance(date, str) or not DATE_PATTERN.fullmatch(date):
        raise AssumptionManifestError("invalid manifest date")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as error:
        raise AssumptionManifestError("invalid manifest date") from error
    if not isinstance(records, list) or not records:
        raise AssumptionManifestError("records must be a non-empty array")
    return AssumptionManifest(date=date, records=tuple(_parse_record(record) for record in records))


def run_live_probe(
    argv: Sequence[str],
    *,
    allow_live: bool,
    run: RunCommand = subprocess.run,
) -> LiveProbeResult:
    if not allow_live:
        raise AssumptionManifestError("live probes require explicit opt-in")
    command = tuple(argv)
    if command not in ALLOWED_LIVE_PROBES:
        raise AssumptionManifestError("live probe command is not allowed")
    try:
        result = run(
            command,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env={},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AssumptionManifestError("live probe could not be executed") from error
    return LiveProbeResult(
        returncode=result.returncode,
        stdout=SECRET_PATTERN.sub(REDACTED, result.stdout),
        stderr=SECRET_PATTERN.sub(REDACTED, result.stderr),
    )
