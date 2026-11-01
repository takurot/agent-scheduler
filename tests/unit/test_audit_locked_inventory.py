from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.audit_locked_inventory import AuditIsolationError, derive_locked_inventory


def test_derives_inventory_without_mounting_or_executing_project(tmp_path: Path) -> None:
    lock = b"lock content\n"
    project = b'[project]\nname = "fixture-project"\nversion = "1.0"\n'
    build = b"fixed build requirements\n"
    (tmp_path / "uv.lock").write_bytes(lock)
    (tmp_path / "pyproject.toml").write_bytes(project)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "offline-build-requirements.txt").write_bytes(build)
    image = "example/image@sha256:" + "a" * 64
    manifest = {
        "version": 1,
        "lock_sha256": hashlib.sha256(lock).hexdigest(),
        "project_sha256": hashlib.sha256(project).hexdigest(),
        "build_requirements_sha256": hashlib.sha256(build).hexdigest(),
        "requirements_sha256": "b" * 64,
        "identity": {"python": "3.12.12", "platform": "linux-aarch64", "uv": "uv 0.9.30"},
    }
    result_data = {
        "manifest": manifest,
        "identity": manifest["identity"],
        "requirements_sha256": "b" * 64,
        "export_sha256": "b" * 64,
        "build_requirements_sha256": hashlib.sha256(build).hexdigest(),
        "packages": [["one", "2.0"], ["two", "3.0"]],
    }

    def fake_runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        assert (
            "--network" in request.argv
            and request.argv[request.argv.index("--network") + 1] == "none"
        )
        assert "--read-only" in request.argv and "--mount" not in request.argv
        assert "--interactive" in request.argv
        assert str(tmp_path) not in " ".join(request.argv)
        assert "/tmp/venv/bin/python -S" in request.argv[-1]
        assert "--no-build" in request.argv[-1]
        supplied = json.loads(request.stdin_payload)
        assert base64.b64decode(supplied["lock"]) == lock
        assert base64.b64decode(supplied["project"]) == project
        return ProcessExecutionResult(exit_code=0, stdout=json.dumps(result_data))

    assert derive_locked_inventory(
        tmp_path,
        image,
        Path("/usr/bin/true"),
        runner=fake_runner,
        cleanup=lambda *args, **kwargs: None,
    ) == [("fixture-project", "1.0"), ("one", "2.0"), ("two", "3.0")]
    with pytest.raises(AuditIsolationError):
        derive_locked_inventory(
            tmp_path,
            image,
            Path("/usr/bin/true"),
            runner=fake_runner,
            cleanup=lambda *args, **kwargs: "cleanup unconfirmed",
        )
    result_data["manifest"]["lock_sha256"] = "f" * 64
    with pytest.raises(ValueError):
        derive_locked_inventory(
            tmp_path,
            image,
            Path("/usr/bin/true"),
            runner=fake_runner,
            cleanup=lambda *args, **kwargs: None,
        )
    result_data["manifest"]["lock_sha256"] = hashlib.sha256(lock).hexdigest()
    result_data["manifest"]["identity"] = {}
    result_data["identity"] = {}
    with pytest.raises(ValueError):
        derive_locked_inventory(
            tmp_path,
            image,
            Path("/usr/bin/true"),
            runner=fake_runner,
            cleanup=lambda *args, **kwargs: None,
        )
