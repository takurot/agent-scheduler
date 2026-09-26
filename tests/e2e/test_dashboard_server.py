from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from subsched.dashboard.server import run_dashboard_server
from subsched.models import Task, TaskState
from subsched.storage import JsonStateStore


@pytest.fixture
def dashboard(tmp_path: Path):
    store = JsonStateStore(tmp_path)
    store.init_directories()
    task = Task(
        task_id="github-1",
        issue_number=1,
        title="Task 1",
        labels=(),
        status=TaskState.IN_PROGRESS,
    )
    store.save_tasks((task,))
    handoff_path = store.handoffs_dir / "1.md"
    handoff_path.write_text("# Issue\n#1 Task 1\n\nhandoff body\n", encoding="utf-8")

    token = "test-token-value"
    server = run_dashboard_server(tmp_path, host="127.0.0.1", port=0, interval=2.0, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(
    server, path: str, token: str | None, *, host_header: str | None = None
) -> tuple[int, dict]:
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        headers = {}
        if host_header is not None:
            headers["Host"] = host_header
        query = f"?token={token}" if token is not None else ""
        conn.request("GET", f"{path}{query}", headers=headers)
        response = conn.getresponse()
        body = response.read()
        status = response.status
        response_headers = dict(response.getheaders())
    finally:
        conn.close()
    payload = json.loads(body) if body and response_headers.get("Content-Type", "").startswith(
        "application/json"
    ) else {}
    return status, payload, response_headers


def test_status_endpoint_returns_200_with_security_headers(dashboard) -> None:
    server, token = dashboard
    status, payload, headers = _get(server, "/api/status", token)

    assert status == 200
    assert payload["queue_size"] == 1
    for header in (
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Cache-Control",
    ):
        assert header in headers


def test_root_serves_html_with_valid_token(dashboard) -> None:
    server, token = dashboard
    status, _, headers = _get(server, "/", token)

    assert status == 200
    assert headers["Content-Type"].startswith("text/html")


def test_missing_token_is_forbidden(dashboard) -> None:
    server, _token = dashboard
    status, _, _ = _get(server, "/api/status", None)
    assert status == 403

    status_root, _, _ = _get(server, "/", None)
    assert status_root == 403


def test_wrong_token_is_forbidden(dashboard) -> None:
    server, _token = dashboard
    status, _, _ = _get(server, "/api/status", "wrong-token")
    assert status == 403


def test_non_ascii_token_is_forbidden_not_a_crash(dashboard) -> None:
    server, _token = dashboard
    status, _, _ = _get(server, "/api/status", "%C3%A9")
    assert status == 403


def test_forged_host_header_is_rejected(dashboard) -> None:
    server, token = dashboard
    status, _, _ = _get(server, "/api/status", token, host_header="evil.example:80")
    assert status == 400


def test_task_detail_invalid_issue_returns_404(dashboard) -> None:
    server, token = dashboard
    status, _, _ = _get(server, "/api/tasks/abc", token)
    assert status == 404

    status_neg, _, _ = _get(server, "/api/tasks/-1", token)
    assert status_neg == 404


def test_task_detail_valid_issue_returns_handoff(dashboard) -> None:
    server, token = dashboard
    status, payload, _ = _get(server, "/api/tasks/1", token)
    assert status == 200
    assert payload["issue_number"] == 1
    assert "handoff body" in payload["handoff"]


def test_task_detail_unknown_issue_returns_404(dashboard) -> None:
    server, token = dashboard
    status, _, _ = _get(server, "/api/tasks/999", token)
    assert status == 404


def test_tasks_capacity_and_metrics_endpoints_return_200(dashboard) -> None:
    server, token = dashboard
    for path in ("/api/tasks", "/api/capacity", "/api/metrics"):
        status, payload, _ = _get(server, path, token)
        assert status == 200, path
        assert "revision" in payload, path


def test_unknown_path_returns_404(dashboard) -> None:
    server, token = dashboard
    status, _, _ = _get(server, "/does-not-exist", token)
    assert status == 404


def test_missing_host_header_is_rejected(dashboard) -> None:
    server, token = dashboard
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", f"/api/status?token={token}", skip_host=True)
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        assert response.status == 400
    finally:
        conn.close()


@pytest.mark.parametrize("method", ["PUT", "DELETE", "PATCH"])
def test_other_mutating_methods_are_rejected(dashboard, method: str) -> None:
    server, token = dashboard
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, f"/api/status?token={token}")
        response = conn.getresponse()
        response.read()
        assert response.status == 405
    finally:
        conn.close()


def test_uninitialized_repository_returns_error_payload(tmp_path: Path) -> None:
    server = run_dashboard_server(tmp_path, host="127.0.0.1", port=0, interval=2.0, token="tok")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload, _ = _get(server, "/api/status", "tok")
        assert status == 200
        assert payload["error"] == "uninitialized"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_corrupted_state_returns_error_payload(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks(())
    store._write_recovery_marker("test corruption", store.path)

    server = run_dashboard_server(tmp_path, host="127.0.0.1", port=0, interval=2.0, token="tok")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, payload, _ = _get(server, "/api/status", "tok")
        assert status == 200
        assert payload["error"] == "state_corrupted"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_mutating_methods_are_rejected(dashboard) -> None:
    server, token = dashboard
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", f"/api/status?token={token}")
        response = conn.getresponse()
        response.read()
        assert response.status == 405
    finally:
        conn.close()


def test_server_never_acquires_scheduler_lock(dashboard) -> None:
    server, _token = dashboard
    lock_file = server.dashboard_repository / ".ai" / "scheduler.lock"
    assert not lock_file.exists()
