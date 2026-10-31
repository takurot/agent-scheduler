"""Prepare a hash-checked, networkless Python verification environment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import sysconfig
import tomllib
from pathlib import Path
from urllib.parse import urlsplit


def _read_file(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing or unsafe bundle file: {path.name}")
    return path.read_bytes()


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _wheel_hashes(
    lock: dict, build_requirements: bytes, project_name: str = ""
) -> dict[str, tuple[str, int | None]]:
    allowed: dict[str, tuple[str, int | None]] = {}
    expected_build = {
        b"editables==0.5 --hash=sha256:"
        b"61e5ffa82629e0d8bfe09bc44a07db3c1ab8ed1ce78a6980732870f19b5e7d4c",
        b"hatchling==1.27.0 --hash=sha256:"
        b"d3a2f3567c4f926ea39849cdf924c7e99e6686c9c8e288ae1037c8fa2a5d937b",
        b"trove-classifiers==2026.9.21.13 --hash=sha256:"
        b"8b1ff4f9c191b1040b71c37f1e445ab99732911e3cd91de52838453a854d7a17",
    }
    lines = [
        line.strip()
        for line in build_requirements.splitlines()
        if line.strip() and not line.lstrip().startswith(b"#")
    ]
    if len(lines) != len(expected_build) or set(lines) != expected_build:
        raise ValueError("unsupported build requirements")
    for package in lock["package"]:
        source = package["source"]
        if source not in ({"registry": "https://pypi.org/simple"}, {"editable": "."}):
            raise ValueError("unsupported offline dependency source")
        if source == {"editable": "."} and package["name"] != project_name:
            raise ValueError("unsupported editable dependency")
        for wheel in package.get("wheels", []):
            url = urlsplit(wheel["url"])
            if (
                url.scheme != "https"
                or url.netloc != "files.pythonhosted.org"
                or url.query
                or url.fragment
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", wheel["hash"])
                or type(wheel.get("size")) is not int
                or not 0 < wheel["size"] <= 1024 * 1024 * 1024
            ):
                raise ValueError("unsupported locked wheel")
            name = url.path.rsplit("/", 1)[-1]
            if not name.endswith(".whl") or name in allowed:
                raise ValueError("invalid or duplicate wheel name")
            allowed[name] = (wheel["hash"][7:], wheel["size"])
    # Build tools are pinned separately because uv.lock does not lock PEP 517 build requirements.
    build_hashes = set(re.findall(rb"--hash=sha256:([0-9a-f]{64})", build_requirements))
    for name, digest in BUILD_WHEELS.items():
        if digest.encode() not in build_hashes:
            raise ValueError("build wheel hash is not pinned")
        allowed[name] = (digest, None)
    return allowed


BUILD_WHEELS = {
    "editables-0.5-py3-none-any.whl": (
        "61e5ffa82629e0d8bfe09bc44a07db3c1ab8ed1ce78a6980732870f19b5e7d4c"
    ),
    "hatchling-1.27.0-py3-none-any.whl": (
        "d3a2f3567c4f926ea39849cdf924c7e99e6686c9c8e288ae1037c8fa2a5d937b"
    ),
    "trove_classifiers-2026.9.21.13-py3-none-any.whl": (
        "8b1ff4f9c191b1040b71c37f1e445ab99732911e3cd91de52838453a854d7a17"
    ),
}


def validate_bundle(
    repo: Path, bundle: Path, identity: dict[str, str], exported_requirements: bytes
) -> None:
    if bundle.is_symlink() or not bundle.is_dir():
        raise ValueError("offline bundle must be a real directory")
    wheels_dir = bundle / "wheels"
    if wheels_dir.is_symlink() or not wheels_dir.is_dir():
        raise ValueError("offline wheelhouse must be a real directory")
    manifest = json.loads(_read_file(bundle / "manifest.json"))
    if not isinstance(manifest, dict):
        raise ValueError("offline bundle manifest is invalid")
    if (repo / "scripts").is_symlink():
        raise ValueError("offline build requirements directory is unsafe")
    lock_bytes = _read_file(repo / "uv.lock")
    project_bytes = _read_file(repo / "pyproject.toml")
    build_bytes = _read_file(repo / "scripts" / "offline-build-requirements.txt")
    requirements = _read_file(bundle / "requirements.txt")
    bundle_build = _read_file(bundle / "build-requirements.txt")
    expected = {
        "version": 1,
        "lock_sha256": _digest(lock_bytes),
        "project_sha256": _digest(project_bytes),
        "requirements_sha256": _digest(requirements),
        "build_requirements_sha256": _digest(build_bytes),
        "identity": identity,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise ValueError("offline bundle is stale or has a different runtime")
    if requirements != exported_requirements or bundle_build != build_bytes:
        raise ValueError("offline requirements do not match the current lock and build tools")
    project = tomllib.loads(project_bytes.decode())
    allowed = _wheel_hashes(
        tomllib.loads(lock_bytes.decode()), build_bytes, project["project"]["name"]
    )
    wheels = manifest.get("wheels")
    if not isinstance(wheels, dict) or not wheels:
        raise ValueError("offline bundle has no wheel manifest")
    if set(wheels) != {entry.name for entry in wheels_dir.iterdir()}:
        raise ValueError("offline wheelhouse contents do not match the manifest")
    for name, digest in wheels.items():
        if not isinstance(name, str) or Path(name).name != name or name not in allowed:
            raise ValueError("offline wheel name is not allowed")
        expected_hash, expected_size = allowed[name]
        data = _read_file(wheels_dir / name)
        if digest != expected_hash or _digest(data) != expected_hash:
            raise ValueError("offline wheel hash mismatch")
        if expected_size is not None and len(data) != expected_size:
            raise ValueError("offline wheel size mismatch")


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, cwd=cwd, env=env, capture_output=True, check=True, timeout=120)


def prepare(repo: Path, bundle: Path, venv: Path, cache: Path) -> None:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(venv.parent),
        "UV_CACHE_DIR": str(cache),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
    }
    uv_version = _run(["uv", "--version"], cwd=repo, env=env).stdout.decode().strip()
    identity = {
        "python": sys.version.split()[0],
        "platform": sysconfig.get_platform(),
        "uv": uv_version,
    }
    requirements = _run(
        [
            "uv",
            "export",
            "--frozen",
            "--offline",
            "--no-config",
            "--no-emit-project",
            "--no-header",
            "--no-annotate",
        ],
        cwd=repo,
        env=env,
    ).stdout
    validate_bundle(repo, bundle, identity, requirements)
    common = [
        "--python",
        str(venv / "bin" / "python"),
        "--offline",
        "--no-index",
        "--find-links",
        str(bundle / "wheels"),
        "--require-hashes",
        "--no-cache",
        "--no-config",
    ]
    _run(
        ["uv", "venv", str(venv), "--python", sys.executable, "--offline", "--no-config"],
        cwd=repo,
        env=env,
    )
    _run(["uv", "pip", "sync", *common, str(bundle / "requirements.txt")], cwd=repo, env=env)
    _run(
        [
            "uv",
            "pip",
            "install",
            *common,
            "--no-deps",
            "-r",
            str(bundle / "build-requirements.txt"),
        ],
        cwd=repo,
        env=env,
    )
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            "--offline",
            "--no-build-isolation",
            "--no-deps",
            "--no-cache",
            "--no-config",
            "-e",
            str(repo),
        ],
        cwd=repo,
        env=env,
    )


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: offline_verification.py BUNDLE VENV CACHE")
    try:
        prepare(Path.cwd(), Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"offline Python preparation failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from error
