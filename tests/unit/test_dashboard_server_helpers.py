from __future__ import annotations

import socket
from pathlib import Path

import pytest

from subsched.dashboard.server import build_dashboard_server, find_free_port_from, resolve_port


def test_resolve_port_returns_zero_unchanged() -> None:
    assert resolve_port("127.0.0.1", 0) == 0


def test_resolve_port_returns_requested_port_when_free() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    assert resolve_port("127.0.0.1", free_port) == free_port


def test_resolve_port_finds_next_free_port_when_busy() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        busy_port = busy.getsockname()[1]
        resolved = resolve_port("127.0.0.1", busy_port)
        assert resolved != busy_port


def test_find_free_port_from_raises_when_exhausted() -> None:
    sockets = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            start_port = probe.getsockname()[1]
        for offset in range(3):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", start_port + offset))
            s.listen(1)
            sockets.append(s)

        with pytest.raises(OSError):
            find_free_port_from("127.0.0.1", start_port, max_attempts=3)
    finally:
        for s in sockets:
            s.close()


def test_build_dashboard_server_generates_token_and_binds(tmp_path: Path) -> None:
    server = build_dashboard_server(tmp_path, host="127.0.0.1", port=0, interval=1.5)
    try:
        assert server.dashboard_token
        assert server.dashboard_repository == tmp_path
        assert server.server_address[1] != 0
    finally:
        server.server_close()
