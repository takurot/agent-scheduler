"""Public validation diagnostics must survive the real MCP stdio transport."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from subsched.storage import JsonStateStore

# Run the actual CLI with guards against discovery and worker/process launch.
# Fault injection stays in the test child, never in the production CLI surface.
SERVER = """
import sys
from pathlib import Path
from types import SimpleNamespace
import subsched.mcp_server as server
from subsched.cli import app

fault = sys.argv.pop(1)
def forbidden(*args, **kwargs):
    Path('forbidden-call').write_text('unexpected side effect')
    raise AssertionError('discovery or process launch attempted')
server.subprocess = SimpleNamespace(Popen=forbidden, run=forbidden)
server.GitHubIssueSource.list_open = forbidden
if fault != 'none':
    def fail(*args, **kwargs):
        message = 'synthetic-secret-541 provider raw output /private/host/credential'
        if fault == 'wrapped':
            raise server.McpToolError(message)
        raise RuntimeError(message)
    server.load_config = fail
app()
"""


def _call(repository: Path, tool: str, arguments: dict[str, Any], fault: str = "none") -> Any:
    async def call() -> Any:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-c", SERVER, fault, "--repository", str(repository), "mcp"],
            cwd=repository,
        )
        with anyio.fail_after(20):
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                # A subsequent protocol request must still work after the error.
                assert (await session.list_tools()).tools
                return result

    return asyncio.run(call())


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    (tmp_path / "subsched.yaml").write_text(
        'github:\n  repo: owner/project\nverification:\n'
        '  commands: ["python -c pass"]\n  required_commands: ["pytest"]\n'
    )
    JsonStateStore(tmp_path).save_tasks(())
    return tmp_path


def _state(repository: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(repository)): path.read_bytes()
        for path in (repository / ".ai").rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    ("tool", "arguments", "reason_code", "guidance"),
    [
        (
            "subsched_trigger_dispatch",
            {"allow_native": True, "subscription_billing_verified": False},
            "subscription_billing_unverified",
            "subscription_billing_verified=True",
        ),
        (
            "subsched_queue_issues",
            {"issues": "1001", "dry_run": True},
            "invalid_queue_configuration",
            "verification.required_commands",
        ),
        (
            "subsched_queue_issues",
            {"issues": "1001", "dry_run": False},
            "invalid_queue_configuration",
            "verification.required_commands",
        ),
    ],
)
def test_stdio_validation_refusals(
    repository: Path, tool: str, arguments: dict[str, Any], reason_code: str, guidance: str
) -> None:
    before = _state(repository)
    result = _call(repository, tool, arguments)
    assert result.is_error
    content = result.model_dump_json()
    assert reason_code in content
    assert guidance in content
    assert str(repository) not in content
    assert _state(repository) == before
    assert not (repository / "forbidden-call").exists()


@pytest.mark.parametrize("fault", ["runtime", "wrapped"])
def test_stdio_unknown_errors_stay_masked(repository: Path, fault: str) -> None:
    before = _state(repository)
    result = _call(
        repository, "subsched_queue_issues", {"issues": "1001", "dry_run": True}, fault
    )
    assert result.is_error
    content = result.model_dump_json()
    for private in (
        "synthetic-secret-541", "provider raw output", "/private/host", str(repository)
    ):
        assert private not in content
    assert "invalid_queue_configuration" not in content
    assert _state(repository) == before
    assert not (repository / "forbidden-call").exists()


def test_stdio_invalid_config_does_not_publish_raw_values(repository: Path) -> None:
    (repository / "subsched.yaml").write_text(
        'github:\n  repo: owner/project\nsynthetic-secret-541: /private/host/credential\n'
    )
    result = _call(repository, "subsched_queue_issues", {"issues": "1001", "dry_run": True})
    assert result.is_error
    content = result.model_dump_json()
    assert "invalid_queue_configuration" in content
    assert "synthetic-secret-541" not in content
    assert "/private/host" not in content
