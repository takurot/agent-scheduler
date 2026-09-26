from __future__ import annotations

import contextlib
import json
import re
import secrets
import socket
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from subsched.dashboard import api
from subsched.storage import JsonStateStore, SchedulerStateSnapshot, StateCorruptionError

_ISSUE_RE = re.compile(r"^[1-9]\d*$")
_TASK_DETAIL_RE = re.compile(r"^/api/tasks/([^/]+)$")

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; "
        "frame-ancestors 'none';"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _load_index_html() -> str:
    return (
        resources.files("subsched.dashboard")
        .joinpath("static/index.html")
        .read_text(encoding="utf-8")
    )


def _valid_host(host_header: str | None, port: int) -> bool:
    """Accept only `127.0.0.1`/`localhost`, with or without an explicit `:<port>`
    suffix matching the server's actual bound port, rejecting everything else
    (DNS rebinding / cross-origin protection)."""
    if not host_header:
        return False
    candidate = host_header.strip().lower()
    allowed = {"127.0.0.1", "localhost", f"127.0.0.1:{port}", f"localhost:{port}"}
    return candidate in allowed


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        repository: Path,
        interval: float,
        token: str,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.dashboard_repository = repository
        self.dashboard_interval = interval
        self.dashboard_token = token


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardServer

    def _send_security_headers(self) -> None:
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_html(
        self, status: HTTPStatus, body: bytes, *, script_nonce: str | None = None
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            if name == "Content-Security-Policy" and script_nonce is not None:
                value = f"script-src 'self' 'nonce-{script_nonce}'; " + value
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _reject(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _check_host(self) -> bool:
        return _valid_host(self.headers.get("Host"), self.server.server_address[1])

    def _check_token(self, query: dict[str, list[str]]) -> bool:
        supplied = query.get("token", [""])[0]
        if not supplied.isascii():
            return False
        return secrets.compare_digest(supplied, self.server.dashboard_token)

    def _load_snapshot(self) -> SchedulerStateSnapshot | None:
        """Returns None (after sending a 200 error payload) for uninitialized or
        corrupted state, so callers never need to distinguish those from a genuine
        empty snapshot."""
        store = JsonStateStore(self.server.dashboard_repository)
        if not store.state_dir.exists():
            self._send_json(HTTPStatus.OK, {"error": "uninitialized"})
            return None
        try:
            return store.load_snapshot()
        except StateCorruptionError:
            self._send_json(HTTPStatus.OK, {"error": "state_corrupted"})
            return None

    def _serve_snapshot(self, builder: Callable[[SchedulerStateSnapshot], dict[str, Any]]) -> None:
        snapshot = self._load_snapshot()
        if snapshot is None:
            return
        self._send_json(HTTPStatus.OK, builder(snapshot))

    def _serve_task_detail(self, issue: int) -> None:
        snapshot = self._load_snapshot()
        if snapshot is None:
            return
        detail = api.build_task_detail(snapshot, self.server.dashboard_repository, issue)
        if detail is None:
            self._reject(HTTPStatus.NOT_FOUND, "task not found")
            return
        self._send_json(HTTPStatus.OK, detail)

    def _serve_index(self) -> None:
        nonce = secrets.token_urlsafe(16)
        html = (
            _load_index_html()
            .replace("__DASHBOARD_INTERVAL_PLACEHOLDER__", repr(self.server.dashboard_interval))
            .replace("__DASHBOARD_NONCE_PLACEHOLDER__", nonce)
        )
        self._send_html(HTTPStatus.OK, html.encode("utf-8"), script_nonce=nonce)

    def do_GET(self) -> None:
        if not self._check_host():
            self._reject(HTTPStatus.BAD_REQUEST, "invalid host header")
            return
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if not self._check_token(query):
            self._reject(HTTPStatus.FORBIDDEN, "invalid or missing token")
            return

        path = parts.path
        if path == "/":
            self._serve_index()
            return
        if path == "/api/status":
            self._serve_snapshot(api.build_status)
            return
        if path == "/api/tasks":
            self._serve_snapshot(api.build_tasks)
            return
        if path == "/api/capacity":
            self._serve_snapshot(api.build_capacity)
            return
        if path == "/api/metrics":
            self._serve_snapshot(api.build_metrics)
            return
        match = _TASK_DETAIL_RE.match(path)
        if match:
            raw_issue = match.group(1)
            if not _ISSUE_RE.match(raw_issue):
                self._reject(HTTPStatus.NOT_FOUND, "not found")
                return
            self._serve_task_detail(int(raw_issue))
            return
        self._reject(HTTPStatus.NOT_FOUND, "not found")

    def _reject_mutation(self) -> None:
        self._reject(HTTPStatus.METHOD_NOT_ALLOWED, "read-only dashboard: method not allowed")

    def do_POST(self) -> None:
        self._reject_mutation()

    def do_PUT(self) -> None:
        self._reject_mutation()

    def do_DELETE(self) -> None:
        self._reject_mutation()

    def do_PATCH(self) -> None:
        self._reject_mutation()

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress default stderr access logging: request lines include the ephemeral
        # token as a query parameter, which must never be written to a shared log.
        pass


def run_dashboard_server(
    repository: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    interval: float = 2.0,
    token: str | None = None,
) -> DashboardServer:
    effective_token = token or secrets.token_urlsafe(16)
    return DashboardServer(
        (host, port),
        DashboardRequestHandler,
        repository=repository,
        interval=interval,
        token=effective_token,
    )


def find_free_port_from(host: str, start_port: int, *, max_attempts: int = 20) -> int:
    for candidate in range(start_port, start_port + max_attempts):
        with (
            contextlib.suppress(OSError),
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe,
        ):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, candidate))
            return candidate
    raise OSError(
        f"no free port found in range {start_port}-{start_port + max_attempts - 1} on {host}"
    )


def resolve_port(host: str, requested_port: int) -> int:
    """Return `requested_port` if it's free, otherwise the next free port after it.
    `requested_port == 0` (let the OS choose) is returned unchanged."""
    if requested_port == 0:
        return 0
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, requested_port))
        return requested_port
    return find_free_port_from(host, requested_port + 1)


def build_dashboard_server(
    repository: Path, *, host: str = "127.0.0.1", port: int = 8080, interval: float = 2.0
) -> DashboardServer:
    """Resolve the effective port, generate an ephemeral token, and construct the
    server -- kept separate from `serve_forever()` so it can be exercised directly by
    tests and by the CLI command.
    """
    resolved_port = resolve_port(host, port)
    return run_dashboard_server(repository, host=host, port=resolved_port, interval=interval)
