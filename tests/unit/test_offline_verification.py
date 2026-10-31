from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "offline_verification.py"
SPEC = importlib.util.spec_from_file_location("offline_verification", SCRIPT)
assert SPEC and SPEC.loader
offline = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(offline)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle(tmp_path: Path) -> tuple[Path, Path, dict[str, str], bytes]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "scripts").mkdir()
    wheel = b"synthetic wheel bytes"
    wheel_hash = _sha(wheel)
    lock = (
        'version = 1\n[[package]]\nname = "fixture"\nversion = "1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        f'wheels = [{{ url = "https://files.pythonhosted.org/f/fixture-1.0-py3-none-any.whl", '
        f'hash = "sha256:{wheel_hash}", size = {len(wheel)} }}]\n'
    ).encode()
    project = b'[project]\nname = "fixture"\nversion = "1.0"\n'
    build = (SCRIPT.parent / "offline-build-requirements.txt").read_bytes()
    requirements = f"fixture==1.0 --hash=sha256:{wheel_hash}\n".encode()
    (repo / "uv.lock").write_bytes(lock)
    (repo / "pyproject.toml").write_bytes(project)
    (repo / "scripts" / "offline-build-requirements.txt").write_bytes(build)
    bundle = tmp_path / "bundle"
    (bundle / "wheels").mkdir(parents=True)
    (bundle / "wheels" / "fixture-1.0-py3-none-any.whl").write_bytes(wheel)
    (bundle / "requirements.txt").write_bytes(requirements)
    (bundle / "build-requirements.txt").write_bytes(build)
    identity = {"python": "3.12.12", "platform": "linux-aarch64", "uv": "uv 0.9.30"}
    manifest = {
        "version": 1,
        "lock_sha256": _sha(lock),
        "project_sha256": _sha(project),
        "requirements_sha256": _sha(requirements),
        "build_requirements_sha256": _sha(build),
        "identity": identity,
        "wheels": {"fixture-1.0-py3-none-any.whl": wheel_hash},
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return repo, bundle, identity, requirements


def test_verified_offline_bundle_matches_lock_and_platform(tmp_path: Path) -> None:
    repo, bundle, identity, requirements = _bundle(tmp_path)
    offline.validate_bundle(repo, bundle, identity, requirements)


def test_build_requirements_reject_additional_dependency() -> None:
    build = (SCRIPT.parent / "offline-build-requirements.txt").read_bytes()
    with pytest.raises(ValueError):
        offline._wheel_hashes({"package": []}, build + b"malicious==1\n")


def test_lock_rejects_untrusted_editable_package() -> None:
    build = (SCRIPT.parent / "offline-build-requirements.txt").read_bytes()
    lock = {"package": [{"name": "other", "source": {"editable": "."}}]}
    with pytest.raises(ValueError):
        offline._wheel_hashes(lock, build, "fixture")


@pytest.mark.parametrize(
    "source", [{"git": "https://example.org/repo"}, {"registry": "https://example.org/simple"}]
)
def test_lock_rejects_untrusted_dependency_source(source: dict[str, str]) -> None:
    build = (SCRIPT.parent / "offline-build-requirements.txt").read_bytes()
    with pytest.raises(ValueError):
        offline._wheel_hashes(
            {"package": [{"name": "fixture", "source": source}]}, build, "fixture"
        )


@pytest.mark.parametrize(
    "problem",
    [
        "lock",
        "project",
        "identity",
        "export",
        "wheel_missing",
        "wheel_changed",
        "wheel_symlink",
        "requirements_changed",
        "build_changed",
        "manifest_missing",
    ],
)
def test_offline_bundle_rejects_missing_stale_or_tampered_inputs(
    tmp_path: Path, problem: str
) -> None:
    repo, bundle, identity, requirements = _bundle(tmp_path)
    wheel = bundle / "wheels" / "fixture-1.0-py3-none-any.whl"
    if problem == "lock":
        (repo / "uv.lock").write_text("changed", encoding="utf-8")
    elif problem == "project":
        (repo / "pyproject.toml").write_text("changed", encoding="utf-8")
    elif problem == "identity":
        identity["platform"] = "linux-x86_64"
    elif problem == "export":
        requirements = b"changed"
    elif problem == "wheel_missing":
        wheel.unlink()
    elif problem == "wheel_changed":
        wheel.write_bytes(b"changed")
    elif problem == "wheel_symlink":
        wheel.unlink()
        wheel.symlink_to(repo / "uv.lock")
    elif problem == "requirements_changed":
        (bundle / "requirements.txt").write_bytes(b"changed")
    elif problem == "build_changed":
        (repo / "scripts" / "offline-build-requirements.txt").write_bytes(b"changed")
    else:
        (bundle / "manifest.json").unlink()
    with pytest.raises((ValueError, OSError)):
        offline.validate_bundle(repo, bundle, identity, requirements)
