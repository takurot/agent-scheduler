"""The local quality gate must not repair a stale lockfile while validating it."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import jwt
import pytest
from packaging.version import Version

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quality_gate.sh"


@pytest.mark.parametrize("package,minimum", [("pyjwt", "2.15.1"), ("urllib3", "2.8.0")])
def test_lock_excludes_vulnerable_dependency_versions(package: str, minimum: str) -> None:
    """Issue #449: retain the audited security updates in the frozen environment."""
    with (SCRIPT.parents[1] / "uv.lock").open("rb") as lock_file:
        lock = tomllib.load(lock_file)
    versions = [entry["version"] for entry in lock["package"] if entry["name"] == package]

    assert versions, f"Expected {package} in the MCP/dev dependency graph"
    assert all(Version(version) >= Version(minimum) for version in versions)


def test_pyjwt_preserves_claim_checks_when_options_are_reused() -> None:
    """PYSEC-2026-4146 has no fixed-version metadata: verify the actual behavior."""
    key = "issue-449-test-key-with-at-least-32-bytes"
    token = jwt.encode({"exp": 0}, key, algorithm="HS256")
    options = {"verify_signature": False}

    jwt.decode(token, options=options)

    assert options == {"verify_signature": False}
    options["verify_signature"] = True
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(token, key, algorithms=["HS256"], options=options)


def test_stale_lock_fails_without_rewriting_it(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(SCRIPT, scripts / "quality_gate.sh")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "lock-check-fixture"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )
    subprocess.run(["uv", "lock", "--offline"], cwd=tmp_path, check=True, capture_output=True)
    lock = tmp_path / "uv.lock"
    original = lock.read_bytes()
    pyproject.write_text(
        '[project]\nname = "lock-check-fixture"\nversion = "0.2.0"\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(scripts / "quality_gate.sh")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert lock.read_bytes() == original
