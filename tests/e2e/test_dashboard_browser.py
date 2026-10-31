from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Route, expect, sync_playwright
from typer.testing import CliRunner

from subsched.cli import app
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


def test_dashboard_recovers_after_first_init(browser: Browser, tmp_path: Path) -> None:
    server = run_dashboard_server(tmp_path, port=0, interval=0.2, token="synthetic-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page = browser.new_page()
    try:
        page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=synthetic-token")
        expect(page.locator("#banner")).to_contain_text("subsched init", timeout=5000)
        result = CliRunner().invoke(app, ["init", str(tmp_path), "--repo", "owner/name"])
        assert result.exit_code == 0, result.output
        expect(page.locator("#banner")).to_be_hidden(timeout=5000)
        expect(page.locator("#status-summary")).to_contain_text("Queue size: 0")
        assert not (tmp_path / ".ai").exists()
    finally:
        page.close()
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


def test_selected_detail_updates_when_revision_changes(
    browser: Browser, dashboard_url: str, tmp_path: Path
) -> None:
    store = JsonStateStore(tmp_path)
    handoff = store.handoffs_dir / "1.md"
    handoff.write_text("first handoff", encoding="utf-8")
    page = browser.new_page()
    try:
        page.goto(dashboard_url)
        page.get_by_text("#1 Task 1", exact=True).click(timeout=5000)
        expect(page.locator("#detail-body")).to_contain_text("first handoff")
        handoff.write_text("updated handoff", encoding="utf-8")
        store.set_paused(True)
        expect(page.locator("#status-summary")).to_contain_text("Paused: true", timeout=5000)
        expect(page.locator("#detail-body")).to_contain_text("updated handoff", timeout=5000)
    finally:
        page.close()


@pytest.mark.parametrize("failure", ["missing", "network"])
def test_selected_detail_becomes_unavailable_on_failure(
    browser: Browser, dashboard_url: str, tmp_path: Path, failure: str
) -> None:
    store = JsonStateStore(tmp_path)
    (store.handoffs_dir / "1.md").write_text("old handoff", encoding="utf-8")
    page = browser.new_page()
    failed = {"enabled": False}
    try:
        def handle_detail(route: Route) -> None:
            if failed["enabled"] and failure == "network":
                route.abort("failed")
            else:
                route.continue_()

        page.route("**/api/tasks/1?*", handle_detail)
        page.goto(dashboard_url)
        page.get_by_text("#1 Task 1", exact=True).click(timeout=5000)
        expect(page.locator("#detail-body")).to_contain_text("old handoff")
        failed["enabled"] = True
        if failure == "missing":
            store.save_tasks(())
        else:
            store.set_paused(True)
        expect(page.locator("#detail-body")).to_contain_text("Task #1 is unavailable", timeout=5000)
        expect(page.locator("#detail-body")).not_to_contain_text("old handoff")
        if failure == "network":
            failed["enabled"] = False
            expect(page.locator("#detail-body")).to_contain_text("old handoff", timeout=5000)
    finally:
        page.close()


def test_late_old_detail_cannot_overwrite_new_selection(
    browser: Browser, dashboard_url: str, tmp_path: Path
) -> None:
    store = JsonStateStore(tmp_path)
    task2 = Task(task_id="github-2", issue_number=2, title="Task 2", labels=(),
                 status=TaskState.IN_PROGRESS)
    store.save_tasks((*store.load_tasks(), task2))
    (store.handoffs_dir / "1.md").write_text("first handoff", encoding="utf-8")
    (store.handoffs_dir / "2.md").write_text("second handoff", encoding="utf-8")
    page = browser.new_page()
    pending: list[Route] = []
    try:
        page.route("**/api/tasks/1?*", lambda route: pending.append(route))
        page.goto(dashboard_url)
        page.get_by_text("#1 Task 1", exact=True).click(timeout=5000)
        for _ in range(50):
            if pending:
                break
            page.wait_for_timeout(100)
        assert pending, "first detail request did not start"
        page.get_by_text("#2 Task 2", exact=True).click()
        expect(page.locator("#detail-body")).to_contain_text("second handoff", timeout=5000)
        pending.pop().continue_()
        page.wait_for_timeout(500)
        expect(page.locator("#detail-body")).to_contain_text("#2 Task 2")
        expect(page.locator("#detail-body")).not_to_contain_text("first handoff")
    finally:
        page.close()


def test_new_selection_does_not_block_polling_on_old_detail(
    browser: Browser, dashboard_url: str, tmp_path: Path
) -> None:
    store = JsonStateStore(tmp_path)
    task2 = Task(task_id="github-2", issue_number=2, title="Task 2", labels=(),
                 status=TaskState.IN_PROGRESS)
    store.save_tasks((*store.load_tasks(), task2))
    (store.handoffs_dir / "1.md").write_text("first handoff", encoding="utf-8")
    (store.handoffs_dir / "2.md").write_text("second handoff", encoding="utf-8")
    page = browser.new_page()
    pending: list[Route] = []
    hold = {"enabled": False}
    try:
        def handle_first(route: Route) -> None:
            if hold["enabled"]:
                pending.append(route)
            else:
                route.continue_()

        page.route("**/api/tasks/1?*", handle_first)
        page.goto(dashboard_url)
        page.get_by_text("#1 Task 1", exact=True).click(timeout=5000)
        expect(page.locator("#detail-body")).to_contain_text("first handoff")
        hold["enabled"] = True
        store.set_paused(True)
        for _ in range(50):
            if pending:
                break
            page.wait_for_timeout(100)
        assert pending, "refresh request did not start"
        page.get_by_text("#2 Task 2", exact=True).click()
        expect(page.locator("#detail-body")).to_contain_text("second handoff", timeout=5000)
        store.set_paused(False)
        expect(page.locator("#status-summary")).to_contain_text("Paused: false", timeout=5000)
        expect(page.locator("#detail-body")).to_contain_text("second handoff")
    finally:
        page.close()


def test_stalled_detail_request_becomes_unavailable_and_recovers(
    browser: Browser, dashboard_url: str, tmp_path: Path
) -> None:
    store = JsonStateStore(tmp_path)
    (store.handoffs_dir / "1.md").write_text("first handoff", encoding="utf-8")
    page = browser.new_page()
    pending: list[Route] = []
    hold = {"enabled": False}
    try:
        def handle_detail(route: Route) -> None:
            if hold["enabled"]:
                pending.append(route)
            else:
                route.continue_()

        page.route("**/api/tasks/1?*", handle_detail)
        page.goto(dashboard_url)
        page.get_by_text("#1 Task 1", exact=True).click(timeout=5000)
        expect(page.locator("#detail-body")).to_contain_text("first handoff")
        hold["enabled"] = True
        store.set_paused(True)
        expect(page.locator("#detail-body")).to_contain_text(
            "Task #1 is unavailable", timeout=15000
        )
        expect(page.locator("#banner")).to_contain_text("stale")
        hold["enabled"] = False
        if len(pending) > 1:
            pending[-1].continue_()
        expect(page.locator("#detail-body")).to_contain_text("first handoff", timeout=5000)
    finally:
        page.close()


@pytest.mark.parametrize("key", ["Enter", "Space"])
def test_keyboard_can_open_task_detail(browser: Browser, dashboard_url: str, key: str) -> None:
    page = browser.new_page()
    try:
        page.goto(dashboard_url)
        button = page.get_by_role("button", name="#1 Task 1", exact=True)
        expect(button).to_be_visible(timeout=5000)
        page.keyboard.press("Tab")
        expect(button).to_be_focused()
        assert button.evaluate("el => getComputedStyle(el).outlineStyle") != "none"
        page.keyboard.press(key)
        expect(page.locator("#detail-body")).to_contain_text("#1 Task 1", timeout=5000)
    finally:
        page.close()


def test_keyboard_focus_survives_polling_and_task_update(
    browser: Browser, dashboard_url: str, tmp_path: Path
) -> None:
    from dataclasses import replace

    store = JsonStateStore(tmp_path)
    page = browser.new_page()
    try:
        page.goto(dashboard_url)
        button = page.get_by_role("button", name="#1 Task 1", exact=True)
        expect(button).to_be_visible(timeout=5000)
        page.keyboard.press("Tab")
        expect(button).to_be_focused()
        page.wait_for_timeout(600)
        expect(button).to_be_focused()
        store.set_paused(True)
        expect(page.locator("#status-summary")).to_contain_text("Paused: true", timeout=5000)
        expect(button).to_be_focused()
        updated = replace(store.load_tasks()[0], title="Updated task")
        store.save_tasks((updated,), paused=True)
        updated_button = page.get_by_role("button", name="#1 Updated task", exact=True)
        expect(updated_button).to_be_visible(timeout=5000)
        expect(updated_button).to_be_focused()
    finally:
        page.close()
