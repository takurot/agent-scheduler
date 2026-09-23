"""Opt-in runtime tests for the reference Squid egress policy."""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PUBLIC_TARGET = "93.184.216.10"


def _run(
    docker: Path,
    *args: str,
    check: bool = True,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(docker), *args],
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def proxy_runtime(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, str, str]]:
    if os.environ.get("SUBSCHED_DOCKER_PROXY_TEST") != "1":
        pytest.skip("set SUBSCHED_DOCKER_PROXY_TEST=1 for the real Squid policy test")
    docker_raw = shutil.which("docker")
    if docker_raw is None:
        pytest.fail("docker is required when the real Squid policy test is enabled")

    docker = Path(docker_raw)
    nonce = secrets.token_hex(6)
    image = f"subsched-proxy-policy:{nonce}"
    network = f"subsched-proxy-policy-{nonce}"
    target = f"subsched-proxy-target-{nonce}"
    target_config = tmp_path_factory.mktemp("proxy-policy") / "target.conf"
    target_config.write_text(
        "http_port 443\npid_filename none\nhttp_access deny all\n",
        encoding="utf-8",
    )

    try:
        _run(
            docker,
            "build",
            "-q",
            "-t",
            image,
            "-f",
            "examples/docker/Dockerfile.proxy",
            ".",
        )
        _run(docker, "network", "create", "--subnet", "93.184.216.0/24", network)
        _run(
            docker,
            "run",
            "-d",
            "--name",
            target,
            "--network",
            network,
            "--ip",
            _PUBLIC_TARGET,
            "--mount",
            f"type=bind,src={target_config},dst=/tmp/target.conf,readonly",
            "--entrypoint",
            "squid",
            image,
            "--foreground",
            "-f",
            "/tmp/target.conf",
        )
        yield docker, image, network
    finally:
        _run(docker, "container", "rm", "--force", target, check=False, timeout=30)
        _run(docker, "network", "rm", network, check=False, timeout=30)
        _run(docker, "image", "rm", image, check=False, timeout=30)


def _proxy_status(
    runtime: tuple[Path, str, str],
    *,
    authority: str,
    resolved_address: str,
) -> int:
    docker, image, network = runtime
    name = f"subsched-proxy-policy-{secrets.token_hex(6)}"
    try:
        _run(
            docker,
            "run",
            "-d",
            "--name",
            name,
            "--network",
            network,
            "--add-host",
            f"chatgpt.com:{resolved_address}",
            "-p",
            "127.0.0.1::3128",
            image,
        )
        published = _run(docker, "port", name, "3128/tcp", timeout=30).stdout.strip()
        host, separator, port_raw = published.rpartition(":")
        assert separator and host == "127.0.0.1" and port_raw.isascii() and port_raw.isdigit()
        port = int(port_raw)

        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection((host, port), timeout=1) as connection:
                    request = (
                        f"CONNECT {authority}:443 HTTP/1.1\r\n"
                        f"Host: {authority}:443\r\n\r\n"
                    )
                    connection.sendall(request.encode("ascii"))
                    status_line = connection.recv(4096).split(b"\r\n", 1)[0]
                    return int(status_line.split(b" ", 2)[1])
            except (ConnectionRefusedError, OSError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)
    finally:
        _run(docker, "container", "rm", "--force", name, check=False, timeout=30)


def test_proxy_denies_ip_literal_with_allowed_reverse_name(
    proxy_runtime: tuple[Path, str, str],
) -> None:
    assert (
        _proxy_status(
            proxy_runtime,
            authority=_PUBLIC_TARGET,
            resolved_address=_PUBLIC_TARGET,
        )
        == 403
    )


@pytest.mark.parametrize("prohibited_address", ["127.0.0.1", "10.0.0.10", "169.254.169.254"])
def test_proxy_denies_allowed_name_resolving_to_prohibited_address(
    proxy_runtime: tuple[Path, str, str],
    prohibited_address: str,
) -> None:
    assert (
        _proxy_status(
            proxy_runtime,
            authority="chatgpt.com",
            resolved_address=prohibited_address,
        )
        == 403
    )


def test_proxy_permits_provider_name_resolving_to_public_address(
    proxy_runtime: tuple[Path, str, str],
) -> None:
    assert (
        _proxy_status(
            proxy_runtime,
            authority="chatgpt.com",
            resolved_address=_PUBLIC_TARGET,
        )
        == 200
    )
