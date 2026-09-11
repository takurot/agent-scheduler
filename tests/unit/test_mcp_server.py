"""Unit tests for the MCP server interface (#250): tool functions, resources, and the
`FastMCP` server wiring, exercised directly against `JsonStateStore` fixtures."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from subsched.github.issues import GitHubCliError
from subsched.mcp_server import (
    GUIDELINES,
    McpToolError,
    ServerOptions,
    build_server,
    cancel_task,
    control,
    get_capacities_resource,
    get_guidelines_resource,
    get_metrics,
    get_queue_resource,
    get_status,
    get_task_handoff_resource,
    init_repo,
    inspect_task,
    queue_issues,
    resolve_needs_human,
    resolve_repository,
    trigger_dispatch,
)
from subsched.models import Issue, Task, TaskState
from subsched.storage import JsonStateStore


def _task(issue_number: int, status: TaskState, **kwargs: object) -> Task:
    return Task(
        task_id=f"github-{issue_number}",
        issue_number=issue_number,
        title=f"Task {issue_number}",
        labels=(),
        status=status,
        **kwargs,  # type: ignore[arg-type]
    )


# --- resolve_repository ------------------------------------------------------------


def test_resolve_repository_defaults_to_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resolved = resolve_repository(None)
    assert resolved == tmp_path


def test_resolve_repository_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(McpToolError, match="does not exist"):
        resolve_repository(str(tmp_path / "missing"))


def test_resolve_repository_rejects_symlink(tmp_path: Path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)
    with pytest.raises(McpToolError, match="symlink"):
        resolve_repository(str(link))


# --- get_status ----------------------------------------------------------------


def test_get_status_empty_repository(tmp_path: Path) -> None:
    result = get_status(str(tmp_path))
    assert result["paused"] is False
    assert result["task_counts"] == {}
    assert result["capacities"] == []
    assert "tasks" not in result


def test_get_status_counts_and_verbose(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    tasks = (_task(1, TaskState.READY), _task(2, TaskState.READY), _task(3, TaskState.COMPLETE))
    store.save_tasks(tasks, paused=True)

    result = get_status(str(tmp_path), verbose=True)

    assert result["paused"] is True
    assert result["task_counts"] == {"READY": 2, "COMPLETE": 1}
    assert len(result["tasks"]) == 3
    assert result["tasks"][0]["issue_number"] == 1


# --- inspect_task ----------------------------------------------------------------


def test_inspect_task_not_found(tmp_path: Path) -> None:
    with pytest.raises(McpToolError, match="not in scheduler state"):
        inspect_task(999, str(tmp_path))


def test_inspect_task_with_handoff_and_commits(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(worktree)], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "t@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(worktree), "config", "user.name", "T"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
    )

    handoff_dir = worktree / ".ai" / "handoffs"
    handoff_dir.mkdir(parents=True)
    (handoff_dir / "42.md").write_text(
        "# Issue\n\n#42 Sample\n\n## Goal\n\nDo it\n\n## Current Plan\n\nplan\n\n"
        "## Completed\n\ndone\n\n## Current Work\n\nwork\n\n## Decisions\n\ndecisions\n\n"
        "## Known Broken State\n\nnone\n\n## Next Action\n\nnext\n\n## Timestamp\n\n"
        "2026-01-01T00:00:00Z\n",
        encoding="utf-8",
    )

    task = _task(42, TaskState.NEEDS_HUMAN, worktree=str(worktree))
    store.save_tasks((task,))

    result = inspect_task(42, str(tmp_path))

    assert result["issue_number"] == 42
    assert result["handoff"] is not None
    assert result["handoff"]["goal"] == "Do it"
    assert len(result["recent_commits"]) == 1


def test_inspect_task_git_log_uses_safe_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    task = _task(42, TaskState.NEEDS_HUMAN, worktree=str(worktree))
    store.save_tasks((task,))

    monkeypatch.setenv("GIT_DIR", "/tmp/some-other-repo/.git")
    captured: dict[str, object] = {}
    real_run = subprocess.run

    def _fake_run(argv: list[str], **kwargs: object) -> object:
        if argv[:1] == ["git"]:
            captured["env"] = kwargs.get("env")
            return real_run(argv, **kwargs)  # type: ignore[arg-type]
        return real_run(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("subsched.mcp_server.subprocess.run", _fake_run)

    inspect_task(42, str(tmp_path))

    env = captured["env"]
    assert env is not None
    assert "GIT_DIR" not in env


def test_inspect_task_ignores_symlinked_handoff(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    worktree = tmp_path / "wt"
    (worktree / ".ai" / "handoffs").mkdir(parents=True)
    real = tmp_path / "real_handoff.md"
    real.write_text("not a real handoff", encoding="utf-8")
    (worktree / ".ai" / "handoffs" / "7.md").symlink_to(real)

    task = _task(7, TaskState.NEEDS_HUMAN, worktree=str(worktree))
    store.save_tasks((task,))

    result = inspect_task(7, str(tmp_path))

    assert result["handoff"] is None


# --- queue_issues ----------------------------------------------------------------


def test_queue_issues_requires_github_repo_configured(tmp_path: Path) -> None:
    with pytest.raises(McpToolError, match=r"github\.repo is not configured"):
        queue_issues(str(tmp_path))


def test_queue_issues_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "subsched.yaml").write_text(
        yaml.safe_dump({"github": {"repo": "acme/widgets"}}), encoding="utf-8"
    )
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks((_task(1, TaskState.READY),))

    issues = (Issue(number=1, title="Already queued"), Issue(number=2, title="New one"))
    monkeypatch.setattr(
        "subsched.mcp_server.GitHubIssueSource.list_open", lambda self, repo, label=None: issues
    )

    result = queue_issues(str(tmp_path), dry_run=True)

    assert result == {"discovered": 2, "would_queue": 1, "dry_run": True}


def test_queue_issues_persists_new_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "subsched.yaml").write_text(
        yaml.safe_dump({"github": {"repo": "acme/widgets"}}), encoding="utf-8"
    )
    issues = (Issue(number=5, title="Discovered issue"),)
    monkeypatch.setattr(
        "subsched.mcp_server.GitHubIssueSource.list_open", lambda self, repo, label=None: issues
    )

    result = queue_issues(str(tmp_path))

    assert result == {"discovered": 1, "queued": 1, "dry_run": False}
    store = JsonStateStore(tmp_path)
    assert [task.issue_number for task in store.load_tasks()] == [5]


def test_queue_issues_filters_by_requested_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "subsched.yaml").write_text(
        yaml.safe_dump({"github": {"repo": "acme/widgets"}}), encoding="utf-8"
    )
    issues = (Issue(number=1, title="One"), Issue(number=2, title="Two"))
    monkeypatch.setattr(
        "subsched.mcp_server.GitHubIssueSource.list_open", lambda self, repo, label=None: issues
    )

    result = queue_issues(str(tmp_path), issues="2", dry_run=True)

    assert result["discovered"] == 1


def test_queue_issues_rejects_invalid_issues_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "subsched.yaml").write_text(
        yaml.safe_dump({"github": {"repo": "acme/widgets"}}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "subsched.mcp_server.GitHubIssueSource.list_open", lambda self, repo, label=None: ()
    )

    with pytest.raises(McpToolError, match="all-open"):
        queue_issues(str(tmp_path), issues="not-a-number")


def test_queue_issues_wraps_github_cli_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "subsched.yaml").write_text(
        yaml.safe_dump({"github": {"repo": "acme/widgets"}}), encoding="utf-8"
    )

    def _raise(self: object, repo: str, label: str | None = None) -> tuple[Issue, ...]:
        raise GitHubCliError("gh not authenticated")

    monkeypatch.setattr("subsched.mcp_server.GitHubIssueSource.list_open", _raise)

    with pytest.raises(McpToolError, match="GitHub discovery failed"):
        queue_issues(str(tmp_path))


# --- trigger_dispatch ------------------------------------------------------------


def test_trigger_dispatch_requires_billing_verification_for_native(tmp_path: Path) -> None:
    with pytest.raises(McpToolError, match="subscription_billing_verified"):
        trigger_dispatch(str(tmp_path), allow_native=True, subscription_billing_verified=False)


def test_trigger_dispatch_requires_github_repo_configured(tmp_path: Path) -> None:
    with pytest.raises(McpToolError, match=r"github\.repo is not configured"):
        trigger_dispatch(str(tmp_path))


def test_trigger_dispatch_rejects_invalid_config(tmp_path: Path) -> None:
    (tmp_path / "subsched.yaml").write_text("github: [not, a, mapping]\n", encoding="utf-8")
    with pytest.raises(McpToolError, match=r"invalid subsched\.yaml"):
        trigger_dispatch(str(tmp_path))


def test_trigger_dispatch_launches_detached_dry_run_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "subsched.yaml").write_text(
        yaml.safe_dump({"github": {"repo": "acme/widgets"}}), encoding="utf-8"
    )
    captured: dict[str, object] = {}

    class _FakeProcess:
        pid = 4321

    real_popen = subprocess.Popen

    def _fake_popen(argv: list[str], **kwargs: object) -> object:
        if argv[:2] == [sys.executable, "-m"]:
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            return _FakeProcess()
        return real_popen(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("subsched.mcp_server.subprocess.Popen", _fake_popen)

    result = trigger_dispatch(str(tmp_path))

    assert result["status"] == "dispatched"
    assert result["pid"] == 4321
    assert result["allow_native"] is False
    assert "--dry-run" in captured["argv"]
    assert "--allow-native" not in captured["argv"]
    assert captured["kwargs"]["start_new_session"] is True


def test_trigger_dispatch_native_forwards_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "subsched.yaml").write_text(
        yaml.safe_dump({"github": {"repo": "acme/widgets"}}), encoding="utf-8"
    )
    captured: dict[str, object] = {}

    class _FakeProcess:
        pid = 1

    real_popen = subprocess.Popen

    def _fake_popen(argv: list[str], **kwargs: object) -> object:
        if argv[:2] == [sys.executable, "-m"]:
            captured["argv"] = argv
            return _FakeProcess()
        return real_popen(argv, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("subsched.mcp_server.subprocess.Popen", _fake_popen)

    result = trigger_dispatch(
        str(tmp_path), issues="1,2", allow_native=True, subscription_billing_verified=True
    )

    assert result["allow_native"] is True
    assert "--allow-native" in captured["argv"]
    assert "--subscription-billing-verified" in captured["argv"]
    assert "--issues" in captured["argv"]


# --- init_repo -------------------------------------------------------------------


def test_init_repo_writes_scaffold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from subsched.init import ScaffoldFile, ScaffoldPlan, StackDetection

    plan = ScaffoldPlan(
        stack=StackDetection(name="python", verification_commands=("pytest",)),
        repo="acme/widgets",
        files=(
            ScaffoldFile(
                path=tmp_path / "subsched.yaml", content="github: {}\n", exists=False
            ),
        ),
    )
    monkeypatch.setattr("subsched.mcp_server.build_scaffold_plan", lambda *a, **k: plan)

    result = init_repo(str(tmp_path))

    assert result["repo"] == "acme/widgets"
    assert result["written"] == [str(tmp_path / "subsched.yaml")]
    assert (tmp_path / "subsched.yaml").read_text() == "github: {}\n"


def test_init_repo_wraps_init_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from subsched.init import ScaffoldFile, ScaffoldPlan, StackDetection

    existing = tmp_path / "subsched.yaml"
    existing.write_text("existing", encoding="utf-8")
    plan = ScaffoldPlan(
        stack=StackDetection(name="python", verification_commands=("pytest",)),
        repo="acme/widgets",
        files=(ScaffoldFile(path=existing, content="new", exists=True),),
    )
    monkeypatch.setattr("subsched.mcp_server.build_scaffold_plan", lambda *a, **k: plan)

    with pytest.raises(McpToolError, match="refusing to overwrite"):
        init_repo(str(tmp_path))


# --- resolve_needs_human -----------------------------------------------------------


def test_resolve_needs_human_not_found(tmp_path: Path) -> None:
    with pytest.raises(McpToolError, match="not in scheduler state"):
        resolve_needs_human(1, repository_path=str(tmp_path))


def test_resolve_needs_human_rejects_non_needs_human_task(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks((_task(1, TaskState.READY),))

    with pytest.raises(McpToolError, match="not in NEEDS_HUMAN"):
        resolve_needs_human(1, repository_path=str(tmp_path))


def test_resolve_needs_human_transitions_to_ready(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks((_task(1, TaskState.NEEDS_HUMAN),))

    result = resolve_needs_human(1, resolution_notes="fixed it", repository_path=str(tmp_path))

    assert result == {"issue_number": 1, "status": "READY"}
    reloaded = store.load_tasks()[0]
    assert reloaded.status is TaskState.READY


# --- cancel_task -----------------------------------------------------------------


def test_cancel_task_not_found(tmp_path: Path) -> None:
    with pytest.raises(McpToolError, match="not in scheduler state"):
        cancel_task(1, repository_path=str(tmp_path))


def test_cancel_task_cancels_ready_task(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks((_task(1, TaskState.READY),))

    result = cancel_task(1, repository_path=str(tmp_path))

    assert result == {"issue_number": 1, "status": "CANCELLED", "worktree_preserved": True}
    assert store.load_tasks()[0].status is TaskState.CANCELLED


def test_cancel_task_is_idempotent(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks((_task(1, TaskState.CANCELLED),))

    result = cancel_task(1, repository_path=str(tmp_path))

    assert result["status"] == "CANCELLED"


def test_cancel_task_rejects_invalid_transition(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks((_task(1, TaskState.COMPLETE),))

    with pytest.raises(McpToolError, match="cannot be cancelled"):
        cancel_task(1, repository_path=str(tmp_path))


# --- control -----------------------------------------------------------------


def test_control_pause_and_resume(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()

    assert control("pause", str(tmp_path)) == {"paused": True}
    assert control("resume", str(tmp_path)) == {"paused": False}


# --- get_metrics ---------------------------------------------------------------


def test_get_metrics_returns_expected_shape(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks((_task(1, TaskState.COMPLETE),))

    result = get_metrics(str(tmp_path))

    assert set(result.keys()) == {"productivity", "reliability", "capacity"}


# --- resources -------------------------------------------------------------------


def test_get_queue_resource(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks((_task(1, TaskState.READY),), paused=True)

    result = get_queue_resource(str(tmp_path))

    assert result["paused"] is True
    assert len(result["tasks"]) == 1


def test_get_capacities_resource(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks(
        (),
        paused=False,
    )
    result = get_capacities_resource(str(tmp_path))
    assert result == {"capacities": []}


def test_get_task_handoff_resource_missing(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    store.save_tasks((_task(1, TaskState.READY),))

    with pytest.raises(McpToolError, match="no handoff available"):
        get_task_handoff_resource(1, str(tmp_path))


def test_get_task_handoff_resource_reads_content(tmp_path: Path) -> None:
    store = JsonStateStore(tmp_path)
    store.init_directories()
    worktree = tmp_path / "wt"
    (worktree / ".ai" / "handoffs").mkdir(parents=True)
    (worktree / ".ai" / "handoffs" / "1.md").write_text("# handoff body", encoding="utf-8")
    store.save_tasks((_task(1, TaskState.NEEDS_HUMAN, worktree=str(worktree)),))

    content = get_task_handoff_resource(1, str(tmp_path))

    assert content == "# handoff body"


def test_handoff_resource_rejects_non_integer_issue(tmp_path: Path) -> None:
    server = build_server(ServerOptions(default_repository=tmp_path))

    async def _read() -> object:
        return await server.read_resource("subsched://tasks/abc/handoff")

    # FastMCP's resource manager wraps the underlying `McpToolError` in a `ValueError`;
    # the important behavior is that the raw, unhandled `int()` `ValueError` never
    # propagates -- it's replaced by our own message before FastMCP re-wraps it.
    with pytest.raises(ValueError, match="invalid issue number: abc"):
        asyncio.run(_read())


def test_get_guidelines_resource_mentions_handoff_headers() -> None:
    content = get_guidelines_resource()
    assert content == GUIDELINES
    assert "## Timestamp" in content


# --- build_server ------------------------------------------------------------------


def test_build_server_registers_all_tools_resources_and_prompts(tmp_path: Path) -> None:
    server = build_server(ServerOptions(default_repository=tmp_path))

    async def _introspect() -> tuple[list[str], list[str], list[str]]:
        tools = [tool.name for tool in await server.list_tools()]
        resources = [str(resource.uri) for resource in await server.list_resources()]
        prompts = [prompt.name for prompt in await server.list_prompts()]
        return tools, resources, prompts

    tools, resources, prompts = asyncio.run(_introspect())

    assert set(tools) == {
        "subsched_get_status",
        "subsched_inspect_task",
        "subsched_queue_issues",
        "subsched_trigger_dispatch",
        "subsched_init_repo",
        "subsched_resolve_needs_human",
        "subsched_cancel_task",
        "subsched_control",
        "subsched_get_metrics",
    }
    assert "subsched://queue" in resources
    assert "subsched://capacities" in resources
    assert "subsched://guidelines" in resources
    assert set(prompts) == {"triage_task", "bootstrap_repo"}


def test_build_server_sets_instructions(tmp_path: Path) -> None:
    server = build_server(ServerOptions(default_repository=tmp_path))
    assert server.instructions
    assert "subsched_queue_issues" in server.instructions
    assert "subsched_trigger_dispatch" in server.instructions


def test_build_server_tools_have_descriptions_and_documented_parameters(
    tmp_path: Path,
) -> None:
    server = build_server(ServerOptions(default_repository=tmp_path))

    async def _list_tools() -> list[Any]:
        return await server.list_tools()

    tools = asyncio.run(_list_tools())
    assert tools

    for tool in tools:
        assert tool.description, f"{tool.name} is missing a description"
        properties = tool.inputSchema.get("properties", {})
        for param_name, schema in properties.items():
            assert schema.get(
                "description"
            ), f"{tool.name}.{param_name} is missing a parameter description"


# --- CLI command integration -------------------------------------------------------


def test_cli_mcp_invokes_server_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from subsched.cli import app

    ran = False

    class FakeServer:
        def run(self) -> None:
            nonlocal ran
            ran = True

    monkeypatch.setattr("subsched.mcp_server.build_server", lambda opts: FakeServer())
    runner = CliRunner()
    result = runner.invoke(app, ["--repository", str(tmp_path), "mcp"])

    assert result.exit_code == 0
    assert ran is True


def test_cli_mcp_reports_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    from typer.testing import CliRunner

    from subsched.cli import app

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "subsched.mcp_server":
            raise ImportError("No module named 'mcp'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    runner = CliRunner()
    result = runner.invoke(app, ["--repository", str(tmp_path), "mcp"])

    assert result.exit_code == 1
    assert "The `mcp` package is required" in result.output
