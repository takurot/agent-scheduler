"""Data-only CI audit producer; this module does not establish CI provenance."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from subsched.audit_inventory import canonical_inventory, produce_audit_record

_MAX_INPUT = 65536


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def parse_input(
    target_json: str, inventory_json: str
) -> tuple[dict[str, str], list[tuple[str, str]]]:
    if (
        len(target_json.encode("utf-8")) > _MAX_INPUT
        or len(inventory_json.encode("utf-8")) > _MAX_INPUT
    ):
        raise ValueError("audit input too large")
    target = json.loads(target_json, object_pairs_hook=_unique)
    inventory = json.loads(inventory_json, object_pairs_hook=_unique)
    if (
        not isinstance(target, dict)
        or not all(isinstance(k, str) and isinstance(v, str) for k, v in target.items())
        or not isinstance(inventory, list)
    ):
        raise ValueError("invalid audit input")
    packages = []
    for item in inventory:
        if not isinstance(item, dict) or set(item) != {"name", "version"}:
            raise ValueError("invalid inventory entry")
        packages.append((item["name"], item["version"]))
    canonical_inventory(packages)
    return target, packages


def _write_exclusive(output: Path, data: bytes) -> None:
    if (
        not output.is_absolute()
        or output.is_symlink()
        or any(p.is_symlink() for p in output.parents)
    ):
        raise ValueError("unsafe output path")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def write_record(output: Path, record: dict[str, Any]) -> None:
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    _write_exclusive(output, payload)


def main() -> None:
    try:
        target, packages = parse_input(
            os.environ["SUBSCHED_AUDIT_TARGET"], os.environ["SUBSCHED_AUDIT_INVENTORY"]
        )
        workflow = Path(".github/workflows/independent-audit.yml")
        if workflow.is_symlink() or not workflow.is_file():
            raise ValueError("missing workflow")
        if hashlib.sha256(workflow.read_bytes()).hexdigest() != target.get("workflow_sha256"):
            raise ValueError("workflow mismatch")
        tool = Path(sys.prefix) / "bin" / "pip-audit"
        output = Path(os.environ["SUBSCHED_AUDIT_OUTPUT"])
        if (not output.is_absolute() or output.name != "audit.json"
                or any(parent.is_symlink() for parent in output.parents)):
            raise ValueError("unsafe output path")
        output.parent.mkdir(mode=0o700)
        cache_dir = output.parent / "cache"
        cache_dir.mkdir(mode=0o700)
        record = produce_audit_record(
            packages, target, tool, datetime.now(UTC), cache_dir=cache_dir
        )
        _write_exclusive(output.parent / "inventory.json", canonical_inventory(packages))
        write_record(output, record)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        print("independent audit could not be completed", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
