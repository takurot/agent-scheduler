"""Exercise distribution installs outside the repository with locked runtime dependencies."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


def _run(argv: list[str], cwd: Path) -> str:
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "UV_CACHE_DIR", "UV_LINK_MODE", "UV_PYTHON_DOWNLOADS")
        if key in os.environ
    }
    environment["UV_PROJECT_ENVIRONMENT"] = str(cwd / "venv")
    if os.environ.get("SUBSCHED_OFFLINE_PYTHON") == "1":
        wheelhouse = Path("/opt/subsched-offline/wheels")
        assert not wheelhouse.is_symlink() and wheelhouse.is_dir()
        environment["UV_NO_INDEX"] = "1"
        environment["UV_FIND_LINKS"] = str(wheelhouse)
        environment["UV_OFFLINE"] = "1"
    result = subprocess.run(
        argv, cwd=cwd, env=environment, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def distributions(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, str]:
    root = tmp_path_factory.mktemp("distributions")
    source = root / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md", "uv.lock"):
        shutil.copyfile(_ROOT / name, source / name)
    shutil.copytree(_ROOT / "src", source / "src")
    version = tomllib.loads((source / "pyproject.toml").read_text())["project"]["version"]
    artifacts = root / "dist"
    _run(["uv", "build", "--out-dir", str(artifacts), str(source)], root)
    _run(
        ["uv", "export", "--frozen", "--no-dev", "--no-emit-project",
         "--project", str(source), "--output-file", str(source / "requirements.txt")],
        root,
    )
    return source, artifacts, version


@pytest.mark.parametrize("install_kind", ["wheel", "sdist", "editable"])
def test_installed_version_and_entry_points(
    distributions: tuple[Path, Path, str], tmp_path: Path, install_kind: str
) -> None:
    source, artifacts, expected_version = distributions
    venv = tmp_path / "venv"
    _run(["uv", "venv", "--python", sys.executable, str(venv)], tmp_path)
    python = venv / "bin" / "python"
    # Populate only locked runtime dependencies; do not install the source package.
    _run(
        [
            "uv", "pip", "sync", "--require-hashes", "--python", str(python),
            str(source / "requirements.txt"),
        ],
        tmp_path,
    )
    install = ["uv", "pip", "install", "--no-deps", "--python", str(python)]
    if install_kind == "editable":
        install.extend(["--editable", str(source)])
    else:
        pattern = "*.whl" if install_kind == "wheel" else "*.tar.gz"
        (artifact,) = artifacts.glob(pattern)
        install.append(str(artifact))
    _run(install, tmp_path)
    probe = """
import importlib.metadata
import pathlib
import sys
import subsched
assert subsched.__version__ == importlib.metadata.version("agent-scheduler") == sys.argv[1]
assert pathlib.Path(subsched.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[2]))
"""
    package_root = source if install_kind == "editable" else venv
    _run([str(python), "-I", "-c", probe, expected_version, str(package_root)], tmp_path)
    for command in (
        [str(python), "-I", "-m", "subsched"],
        [str(venv / "bin" / "subsched")],
        [str(venv / "bin" / "agent-scheduler")],
    ):
        assert "Subscription-aware coding agent scheduler" in _run([*command, "--help"], tmp_path)
