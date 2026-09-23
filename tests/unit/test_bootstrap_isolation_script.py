from __future__ import annotations

import os
import stat
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "bootstrap-isolation.sh"
_README = _REPO_ROOT / "README.md"
_SPEC = _REPO_ROOT / "docs" / "SPEC.md"
_SCHEDULER_EXAMPLE = _REPO_ROOT / "examples" / "scheduler.yaml"
_DOCKER_EXAMPLES = _REPO_ROOT / "examples" / "docker"


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


def test_reference_proxy_image_is_fail_closed() -> None:
    """#418: the proxy example is runnable and owns an immutable deny-by-default policy."""
    dockerfile = _DOCKER_EXAMPLES / "Dockerfile.proxy"
    squid_config = _DOCKER_EXAMPLES / "squid.conf"

    assert dockerfile.is_file(), "examples/docker/Dockerfile.proxy must exist"
    assert squid_config.is_file(), "examples/docker/squid.conf must exist"

    dockerfile_text = dockerfile.read_text(encoding="utf-8")
    assert "COPY examples/docker/squid.conf /etc/squid/squid.conf" in dockerfile_text
    assert 'ENTRYPOINT ["squid", "--foreground"' in dockerfile_text
    assert "USER proxy" in dockerfile_text

    policy = squid_config.read_text(encoding="utf-8")
    assert "acl SSL_ports port 443" in policy
    assert "acl CONNECT method CONNECT" in policy
    assert "acl prohibited_destination_ips dst" in policy
    assert "provider_subscription_domains dstdomain -n" in policy
    assert "http_access deny !SSL_ports" in policy
    assert "http_access deny !CONNECT" in policy
    assert "http_access deny prohibited_destination_ips" in policy
    assert "http_access allow CONNECT provider_subscription_domains" in policy
    assert policy.rstrip().endswith("http_access deny all")


def test_reference_proxy_allows_subscription_endpoints_not_openai_api() -> None:
    """#418: Codex ChatGPT auth must work without enabling metered API fallback."""
    policy = (_DOCKER_EXAMPLES / "squid.conf").read_text(encoding="utf-8")

    assert "api.anthropic.com" in policy
    assert "chatgpt.com" in policy
    assert "auth0.openai.com" in policy
    assert "api.openai.com" not in policy


def test_proxy_reference_is_synchronized_across_examples_and_docs() -> None:
    """#418: operator-facing examples identify the reference policy and Codex endpoint."""
    readme_isolation = _README.read_text(encoding="utf-8").split(
        "## Container Isolation Sandbox Architecture", 1
    )[1].split("## Multi-Stage Autonomous Workflow", 1)[0]
    spec_isolation = _SPEC.read_text(encoding="utf-8").split(
        "## 72.2 Native container isolation", 1
    )[1].split("# 73. Success Criteria", 1)[0]
    scheduler_example = _SCHEDULER_EXAMPLE.read_text(encoding="utf-8")

    for content in (readme_isolation, spec_isolation, scheduler_example):
        assert "examples/docker/Dockerfile.proxy" in content
        assert "chatgpt.com" in content
        assert "api.openai.com" not in content
