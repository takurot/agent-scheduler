from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import subsched.cli as cli_module
from subsched.cli import app


def _patch_non_blocking_server(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace `serve_forever` with a no-op so the CLI command returns immediately
    instead of blocking the test forever, while still exercising real port/token
    resolution via the real `build_dashboard_server`."""
    original_build = cli_module.build_dashboard_server

    def _build(*args: object, **kwargs: object):
        server = original_build(*args, **kwargs)  # type: ignore[arg-type]
        # `serve_forever`/`shutdown` coordinate via an internal Event that is only set
        # by a real serve_forever loop; stub both together so `shutdown()` in the
        # command's `finally` block doesn't block waiting for a loop that never ran.
        server.serve_forever = lambda *a, **k: None  # type: ignore[method-assign]
        server.shutdown = lambda *a, **k: None  # type: ignore[method-assign]
        return server

    monkeypatch.setattr(cli_module, "build_dashboard_server", _build)
    opened_urls: list[str] = []
    monkeypatch.setattr(cli_module.webbrowser, "open", opened_urls.append)
    return opened_urls


def test_dashboard_cli_starts_server_prints_url_and_respects_no_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened_urls = _patch_non_blocking_server(monkeypatch)

    result = CliRunner().invoke(
        app,
        ["--repository", str(tmp_path), "dashboard", "--port", "0", "--no-browser"],
    )

    assert result.exit_code == 0, result.output
    assert "Dashboard listening at http://127.0.0.1:" in result.output
    assert "token=" in result.output
    assert opened_urls == []


def test_dashboard_cli_opens_browser_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened_urls = _patch_non_blocking_server(monkeypatch)

    result = CliRunner().invoke(
        app, ["--repository", str(tmp_path), "dashboard", "--port", "0"]
    )

    assert result.exit_code == 0, result.output
    assert len(opened_urls) == 1
    assert opened_urls[0].startswith("http://127.0.0.1:")


@pytest.mark.parametrize("interval", ["0", "-1"])
def test_dashboard_cli_rejects_non_positive_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interval: str
) -> None:
    _patch_non_blocking_server(monkeypatch)

    result = CliRunner().invoke(
        app,
        [
            "--repository",
            str(tmp_path),
            "dashboard",
            "--port",
            "0",
            "--no-browser",
            "--interval",
            interval,
        ],
    )

    assert result.exit_code != 0
    assert "--interval" in result.output
