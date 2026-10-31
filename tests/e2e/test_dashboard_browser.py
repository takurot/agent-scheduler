from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Route, expect, sync_playwright

from subsched.dashboard.server import run_dashboard_server
from subsched.models import Task, TaskState
from subsched.storage import JsonStateStore


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    candidates = (
        shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    )
    executable = next((path for path in candidates if path and Path(path).is_file()), None)
    if executable is None:
        pytest.skip("Chrome or Chromium is required for the dashboard browser test")
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(executable_path=executable, headless=True)
        try:
            yield instance
        finally:
            instance.close()


@pytest.fixture
def dashboard_url(tmp_path: Path) -> Iterator[str]:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks((Task(task_id="github-1", issue_number=1, title="Task 1", labels=(),
                           status=TaskState.IN_PROGRESS),))
    server = run_dashboard_server(tmp_path, port=0, interval=0.2, token="synthetic-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/?token=synthetic-token"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("forbidden", "Authentication expired"),
        ("http", "HTTP 500"),
        ("non_json", "invalid JSON"),
        ("network", "connection failed"),
        ("invalid_payload", "invalid status data"),
        ("invalid_values", "invalid status data"),
        ("uninitialized", "not initialized"),
        ("state_corrupted", "quarantined"),
    ],
)
def test_dashboard_reports_failure_and_recovers_in_browser(
    browser: Browser, dashboard_url: str, failure: str, message: str
) -> None:
    page = browser.new_page()
    try:
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        state = {"failure": "none"}

        def handle_status(route: Route) -> None:
            mode = state["failure"]
            if mode == "forbidden":
                route.fulfill(status=403, json={"error": "forbidden"})
            elif mode == "http":
                route.fulfill(status=500, body="server failure")
            elif mode == "non_json":
                route.fulfill(status=200, content_type="text/html", body="not JSON")
            elif mode == "network":
                route.abort("failed")
            elif mode == "invalid_payload":
                route.fulfill(status=200, json={"unexpected": True})
            elif mode == "invalid_values":
                route.fulfill(status=200, json={
                    "paused": False, "queue_size": -1, "revision": 1, "active_tasks": []
                })
            elif mode in {"uninitialized", "state_corrupted"}:
                route.fulfill(status=200, json={"error": mode})
            else:
                route.continue_()

        page.route("**/api/status?*", handle_status)
        page.goto(dashboard_url)
        expect(page.locator("#status-summary")).to_contain_text("Paused:", timeout=5000)
        expect(page.locator("#kanban")).to_contain_text("#1 Task 1")
        previous_status = page.locator("#status-summary").inner_text()
        state["failure"] = failure
        expect(page.locator("#banner")).to_contain_text(message, timeout=5000)
        expect(page.locator("#banner")).to_contain_text("stale")
        assert page.locator("#status-summary").inner_text() == previous_status
        expect(page.locator("#kanban")).to_contain_text("#1 Task 1")
        state["failure"] = "none"
        expect(page.locator("#banner")).to_be_hidden(timeout=5000)
        expect(page.locator("#status-summary")).to_contain_text("Paused:")
        assert errors == []
    finally:
        page.close()


@pytest.mark.parametrize("endpoint", ["capacity", "metrics"])
@pytest.mark.parametrize("failure", ["network", "invalid_payload"])
def test_secondary_poll_failure_is_reported_and_retried(
    browser: Browser, dashboard_url: str, endpoint: str, failure: str
) -> None:
    page = browser.new_page()
    try:
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        state = {"failed": True}

        def handle_secondary(route: Route) -> None:
            if state["failed"]:
                if failure == "network":
                    route.abort("failed")
                else:
                    route.fulfill(status=200, json={"error": "unexpected"})
            else:
                route.continue_()

        page.route(f"**/api/{endpoint}?*", handle_secondary)
        page.goto(dashboard_url)
        message = "connection failed" if failure == "network" else "invalid data"
        expect(page.locator("#banner")).to_contain_text(message, timeout=5000)
        expect(page.locator("#banner")).to_contain_text("stale")
        state["failed"] = False
        expect(page.locator("#banner")).to_be_hidden(timeout=5000)
        expect(page.locator("#metrics-summary")).to_contain_text("Attempted:")
        assert errors == []
    finally:
        page.close()


def test_recovery_refreshes_capacity_even_when_revision_is_unchanged(
    browser: Browser, dashboard_url: str
) -> None:
    page = browser.new_page()
    try:
        state = {"forbidden": False, "capacity": "AVAILABLE"}

        def handle_status(route: Route) -> None:
            if state["forbidden"]:
                route.fulfill(status=403, json={"error": "forbidden"})
            else:
                route.continue_()

        def handle_capacity(route: Route) -> None:
            route.fulfill(status=200, json={"revision": 1, "capacities": [
                {"agent": "codex", "state": state["capacity"]}
            ]})

        page.route("**/api/status?*", handle_status)
        page.route("**/api/capacity?*", handle_capacity)
        page.goto(dashboard_url)
        expect(page.locator("#capacity-summary")).to_contain_text("codex: AVAILABLE", timeout=5000)
        state["forbidden"] = True
        expect(page.locator("#banner")).to_contain_text("Authentication expired", timeout=5000)
        state["capacity"] = "BUSY"
        state["forbidden"] = False
        expect(page.locator("#banner")).to_be_hidden(timeout=5000)
        expect(page.locator("#capacity-summary")).to_contain_text("codex: BUSY", timeout=5000)
    finally:
        page.close()


def test_stale_warning_remains_until_secondary_data_recovers(
    browser: Browser, dashboard_url: str
) -> None:
    page = browser.new_page()
    try:
        state = {"forbidden": False, "hold_capacity": False}
        pending: list[Route] = []

        def handle_status(route: Route) -> None:
            if state["forbidden"]:
                route.fulfill(status=403, json={"error": "forbidden"})
            else:
                route.continue_()

        def handle_capacity(route: Route) -> None:
            if state["hold_capacity"]:
                pending.append(route)
            else:
                route.continue_()

        page.route("**/api/status?*", handle_status)
        page.route("**/api/capacity?*", handle_capacity)
        page.goto(dashboard_url)
        expect(page.locator("#metrics-summary")).to_contain_text("Attempted:", timeout=5000)
        state["forbidden"] = True
        expect(page.locator("#banner")).to_contain_text("stale", timeout=5000)
        state["hold_capacity"] = True
        state["forbidden"] = False
        for _ in range(50):
            if pending:
                break
            page.wait_for_timeout(100)
        assert pending, "capacity request did not start"
        expect(page.locator("#banner")).to_contain_text("stale")
        state["hold_capacity"] = False
        pending.pop().continue_()
        expect(page.locator("#banner")).to_be_hidden(timeout=5000)
    finally:
        page.close()
