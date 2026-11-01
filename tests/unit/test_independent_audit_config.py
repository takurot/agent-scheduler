from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from subsched.config import ConfigError, _parse_verification_config, load_config


def valid() -> dict:
    return {
        "repository": "owner/repo",
        "repository_id": 11,
        "workflow_id": 22,
        "workflow_commit": "a" * 40,
        "workflow_sha256": "b" * 64,
        "run_id": 33,
        "artifact_id": 44,
        "tool": "pip-audit 2.10.1",
        "policy": "lock-dev-v1",
        "max_age_seconds": 900,
    }


def test_independent_audit_is_opt_in_and_strictly_parsed() -> None:
    assert _parse_verification_config({}).independent_audit is None
    config = _parse_verification_config({"independent_audit": valid()}).independent_audit
    assert config is not None and config.run_id == 33 and config.max_age_seconds == 900
    before_dispatch = valid()
    before_dispatch.pop("run_id")
    before_dispatch.pop("artifact_id")
    pending = _parse_verification_config({"independent_audit": before_dispatch}).independent_audit
    assert pending is not None and pending.run_id is None and pending.artifact_id is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "../.."),
        ("repository_id", True),
        ("workflow_id", 0),
        ("workflow_commit", "main"),
        ("workflow_sha256", "x" * 64),
        ("run_id", -1),
        ("artifact_id", "44"),
        ("tool", "pip-audit latest"),
        ("policy", "bad policy"),
        ("max_age_seconds", 901),
        ("unknown", "x"),
    ],
)
def test_independent_audit_rejects_untrusted_or_ambiguous_configuration(
    field: str, value: object
) -> None:
    data = valid()
    data[field] = value
    with pytest.raises(ConfigError):
        _parse_verification_config({"independent_audit": data})


def test_independent_audit_requires_container_isolation(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        yaml.safe_dump(
            {"github": {"repo": "owner/repo"}, "verification": {"independent_audit": valid()}}
        )
    )
    with pytest.raises(ConfigError, match="container"):
        load_config(path)
