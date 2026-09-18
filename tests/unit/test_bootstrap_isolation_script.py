from __future__ import annotations

import os
import stat
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "bootstrap-isolation.sh"
_README = _REPO_ROOT / "README.md"


def _script_text() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def test_bootstrap_script_exists_and_is_executable() -> None:
    assert _SCRIPT.is_file(), "scripts/bootstrap-isolation.sh must exist"
    mode = os.stat(_SCRIPT).st_mode
    assert mode & stat.S_IXUSR, "script must be executable by the owner"


def test_bootstrap_script_uses_strict_shell_mode() -> None:
    script = _script_text()
    assert script.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script


def test_bootstrap_script_requires_repo_slug_argument() -> None:
    script = _script_text()
    # A positional repo slug (owner/name) must be validated before any docker call.
    assert "repo_slug" in script
    assert "docker" not in script.split("repo_slug", 1)[0]


def test_bootstrap_script_supports_check_only_mode() -> None:
    script = _script_text()
    assert "--check" in script


def test_bootstrap_script_requires_pinned_proxy_image_digest() -> None:
    script = _script_text()
    assert "--proxy-image" in script
    assert "@sha256:" in script


def test_bootstrap_script_fails_closed_on_existing_network() -> None:
    script = _script_text()
    assert "network inspect" in script
    assert "already exists" in script


def test_bootstrap_script_fails_closed_on_existing_proxy_container() -> None:
    script = _script_text()
    assert "container inspect" in script or "ps -a" in script


def test_bootstrap_script_never_deletes_existing_docker_resources() -> None:
    script = _script_text()
    assert "network rm" not in script
    assert "network remove" not in script
    assert "docker rm" not in script
    assert "docker container rm" not in script


def test_bootstrap_script_sets_auth_dir_permissions() -> None:
    script = _script_text()
    assert "chmod 700" in script or "chmod 0700" in script


def test_bootstrap_script_emits_isolation_yaml_block() -> None:
    script = _script_text()
    assert "isolation:" in script
    assert "proxy_url:" in script
    assert "network:" in script
    assert "auth:" in script


def test_readme_documents_bootstrap_script() -> None:
    readme = _README.read_text(encoding="utf-8")
    assert "bootstrap-isolation.sh" in readme


def test_bootstrap_script_supports_language_option() -> None:
    """#364: bootstrap-isolation.sh supports --language to guide toolchain pre-baking."""
    script = _script_text()
    assert "--language" in script
    assert "rust" in script.lower()


def test_reference_worker_dockerfiles_exist() -> None:
    """#364: reference Dockerfiles exist with essential toolchains and procps."""
    docker_dir = _REPO_ROOT / "examples" / "docker"
    rust_dockerfile = docker_dir / "Dockerfile.worker-rust"
    assert rust_dockerfile.is_file(), "examples/docker/Dockerfile.worker-rust must exist"
    rust_content = rust_dockerfile.read_text(encoding="utf-8")
    assert "cargo" in rust_content
    assert "rustfmt" in rust_content
    assert "procps" in rust_content

