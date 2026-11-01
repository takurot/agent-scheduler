"""Prepare a pinned-image build context containing hash-verified Python wheels."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from offline_verification import _digest, _read_file, _wheel_hashes
from packaging.tags import parse_tag
from packaging.utils import parse_wheel_filename

_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}")
_BUILD_URLS = {
    "editables-0.5-py3-none-any.whl": "https://files.pythonhosted.org/packages/6b/be/0f2f4a5e8adc114a02b63d92bf8edbfa24db6fc602fca83c885af2479e0e/editables-0.5-py3-none-any.whl",
    "hatchling-1.27.0-py3-none-any.whl": "https://files.pythonhosted.org/packages/08/e7/ae38d7a6dfba0533684e0b2136817d667588ae3ec984c1a4e5df5eb88482/hatchling-1.27.0-py3-none-any.whl",
    "trove_classifiers-2026.9.21.13-py3-none-any.whl": "https://files.pythonhosted.org/packages/30/81/0da8afb52a71d0a4f2bd3152357b1a441e393b286374802b9d3addab4ab5/trove_classifiers-2026.9.21.13-py3-none-any.whl",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        raise ValueError("wheel download redirected")


def _download(url: str, destination: Path, digest: str, expected_size: int | None) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "files.pythonhosted.org"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("wheel URL is not an approved PyPI artifact")
    if expected_size is not None and not 0 < expected_size <= 1024 * 1024 * 1024:
        raise ValueError("wheel size is outside the supported limit")
    request = urllib.request.Request(url, headers={"User-Agent": "subsched-offline-bundle"})
    hasher = hashlib.sha256()
    count = 0
    partial = destination.with_suffix(".partial")
    with urllib.request.build_opener(_NoRedirect).open(request, timeout=60) as response:
        if response.status != 200:
            raise ValueError("wheel download returned an unexpected status")
        with partial.open("xb") as output:
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > (expected_size or 1024 * 1024 * 1024):
                    raise ValueError("wheel exceeds its declared size")
                output.write(chunk)
                hasher.update(chunk)
    if hasher.hexdigest() != digest or (expected_size is not None and count != expected_size):
        raise ValueError("downloaded wheel failed integrity validation")
    partial.chmod(0o644)
    partial.replace(destination)


def _container(
    image: str, entrypoint: str, args: list[str], *, snapshot: Path | None = None
) -> bytes:
    argv = [
        "docker",
        "run",
        "--rm",
        "--pull",
        "never",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=256m,mode=1777",
        "--env",
        "HOME=/tmp",
        "--env",
        "UV_CACHE_DIR=/tmp/uv-cache",
        "--env",
        "UV_PYTHON_DOWNLOADS=never",
    ]
    if snapshot is not None:
        argv += [
            "--mount",
            f"type=bind,src={snapshot},dst=/project,readonly",
            "--workdir",
            "/project",
        ]
    argv += ["--entrypoint", entrypoint, image, *args]
    return subprocess.run(argv, check=True, capture_output=True, timeout=120).stdout


def prepare_bundle(repo: Path, image: str, destination: Path) -> None:
    if not _IMAGE.fullmatch(image):
        raise ValueError("worker image must be pinned by SHA-256 digest")
    repo = repo.resolve(strict=True)
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if (
        destination == repo
        or destination.is_relative_to(repo)
        or destination.exists()
        or destination.is_symlink()
    ):
        raise ValueError("bundle destination must be new and outside the repository")
    lock_bytes = _read_file(repo / "uv.lock")
    project_bytes = _read_file(repo / "pyproject.toml")
    build_bytes = _read_file(repo / "scripts" / "offline-build-requirements.txt")
    lock = tomllib.loads(lock_bytes.decode())
    project = tomllib.loads(project_bytes.decode())
    allowed = _wheel_hashes(lock, build_bytes, project["project"]["name"])
    destination.mkdir(mode=0o755)
    wheels_dir = destination / "wheels"
    wheels_dir.mkdir(mode=0o755)
    destination.chmod(0o755)
    wheels_dir.chmod(0o755)
    probe = (
        "import json,sys,sysconfig; from pip._vendor.packaging.tags import sys_tags; "
        "print(json.dumps({'python':sys.version.split()[0], "
        "'platform':sysconfig.get_platform(), 'tags':[str(t) for t in sys_tags()]}))"
    )
    metadata = json.loads(_container(image, "python3", ["-c", probe]))
    identity = {
        "python": metadata["python"],
        "platform": metadata["platform"],
        "uv": _container(image, "uv", ["--version"]).decode().strip(),
    }
    with tempfile.TemporaryDirectory(prefix="subsched-offline-snapshot-") as temporary:
        snapshot = Path(temporary)
        (snapshot / "uv.lock").write_bytes(lock_bytes)
        (snapshot / "pyproject.toml").write_bytes(project_bytes)
        requirements = _container(
            image,
            "uv",
            [
                "export",
                "--frozen",
                "--offline",
                "--no-config",
                "--no-emit-project",
                "--no-header",
                "--no-annotate",
            ],
            snapshot=snapshot,
        )
    priority = {next(iter(parse_tag(tag))): index for index, tag in enumerate(metadata["tags"])}
    selected: dict[str, tuple[str, str, int | None]] = {}
    for package in lock["package"]:
        candidates = []
        for wheel in package.get("wheels", []):
            name = urlsplit(wheel["url"]).path.rsplit("/", 1)[-1]
            ranks = [priority[tag] for tag in parse_wheel_filename(name)[3] if tag in priority]
            if ranks:
                candidates.append((min(ranks), name, wheel))
        if candidates:
            _, name, wheel = min(candidates, key=lambda item: item[0])
            selected[name] = (wheel["url"], *allowed[name])
    for name, url in _BUILD_URLS.items():
        selected[name] = (url, *allowed[name])
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_download, url, wheels_dir / name, digest, size)
            for name, (url, digest, size) in selected.items()
        ]
        for future in futures:
            future.result()
    if (
        _read_file(repo / "uv.lock") != lock_bytes
        or _read_file(repo / "pyproject.toml") != project_bytes
    ):
        raise ValueError("project changed during offline bundle preparation")
    if _read_file(repo / "scripts" / "offline-build-requirements.txt") != build_bytes:
        raise ValueError("build requirements changed during offline bundle preparation")
    (destination / "requirements.txt").write_bytes(requirements)
    (destination / "build-requirements.txt").write_bytes(build_bytes)
    manifest = {
        "version": 1,
        "lock_sha256": _digest(lock_bytes),
        "project_sha256": _digest(project_bytes),
        "requirements_sha256": _digest(requirements),
        "build_requirements_sha256": _digest(build_bytes),
        "identity": identity,
        "wheels": {name: digest for name, (_, digest, _) in selected.items()},
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8"
    )
    (destination / "Dockerfile").write_text(
        f"FROM {image}\nCOPY wheels /opt/subsched-offline/wheels\n"
        "COPY requirements.txt build-requirements.txt manifest.json /opt/subsched-offline/\n"
        "ENV SUBSCHED_OFFLINE_PYTHON=1\n",
        encoding="utf-8",
    )
    for name in ("requirements.txt", "build-requirements.txt", "manifest.json", "Dockerfile"):
        (destination / name).chmod(0o644)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Digest-pinned worker image")
    parser.add_argument(
        "--output", required=True, type=Path, help="New bundle directory outside the repository"
    )
    args = parser.parse_args()
    try:
        prepare_bundle(Path.cwd(), args.image, args.output)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(f"offline bundle preparation failed: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1) from error
    print(f"Offline bundle prepared at {args.output}")


if __name__ == "__main__":
    main()
