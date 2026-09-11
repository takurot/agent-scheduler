"""Unit tests for `subsched init` scaffolding (#258): stack detection, GitHub repo
resolution, template rendering, and overwrite protection."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from subsched.config import load_config
from subsched.init import (
    InitError,
    build_scaffold_plan,
    detect_stack,
    render_agent_instructions,
    render_subsched_yaml,
    resolve_github_repo,
    write_scaffold_plan,
)


def _fake_run(returncode: int, stdout: str = "", stderr: str = "") -> Callable[..., object]:
    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)

    return _run


# --- Stack detection -------------------------------------------------------------


def test_detect_stack_python_uv(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "uv.lock").write_text("")

    detection = detect_stack(tmp_path)

    assert detection.name == "python-uv"
    assert "uv run pytest" in detection.verification_commands
    assert "uv run ruff check ." in detection.verification_commands


def test_detect_stack_python_uv_with_src_dir(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "src").mkdir()

    detection = detect_stack(tmp_path)

    assert "uv run mypy src" in detection.verification_commands


def test_detect_stack_python_poetry(tmp_path: Path) -> None:
    (tmp_path / "poetry.lock").write_text("")

    detection = detect_stack(tmp_path)

    assert detection.name == "python-poetry"
    assert "poetry run pytest" in detection.verification_commands


def test_detect_stack_python_plain_requirements(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("")

    detection = detect_stack(tmp_path)

    assert detection.name == "python"
    assert detection.verification_commands == ("pytest", "ruff check .")


def test_detect_stack_go(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")

    detection = detect_stack(tmp_path)

    assert detection.name == "go"
    assert detection.verification_commands == ("go test ./...", "golangci-lint run")


def test_detect_stack_rust(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n")

    detection = detect_stack(tmp_path)

    assert detection.name == "rust"
    assert detection.verification_commands == ("cargo test", "cargo clippy")


def test_detect_stack_node_npm_extracts_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest", "lint": "eslint ."}}')

    detection = detect_stack(tmp_path)

    assert detection.name == "node"
    assert detection.verification_commands == ("npm test", "npm run lint")


def test_detect_stack_node_pnpm(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    (tmp_path / "pnpm-lock.yaml").write_text("")

    detection = detect_stack(tmp_path)

    assert detection.verification_commands == ("pnpm test",)


def test_detect_stack_node_without_scripts_defaults(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}")

    detection = detect_stack(tmp_path)

    assert detection.verification_commands == ("npm test",)


def test_detect_stack_node_with_invalid_json_falls_back(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("not json")

    detection = detect_stack(tmp_path)

    assert detection.verification_commands == ("npm test",)


def test_detect_stack_generic_fallback(tmp_path: Path) -> None:
    detection = detect_stack(tmp_path)

    assert detection.name == "generic"
    assert detection.verification_commands == ("pytest", "ruff check .")


def test_detect_stack_python_takes_priority_over_node(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "package.json").write_text("{}")

    detection = detect_stack(tmp_path)

    assert detection.name == "python"


# --- GitHub repo resolution --------------------------------------------------------


def test_resolve_github_repo_from_https_remote(tmp_path: Path) -> None:
    repo = resolve_github_repo(
        tmp_path, run=_fake_run(0, stdout="https://github.com/owner/name.git\n")
    )

    assert repo == "owner/name"


def test_resolve_github_repo_from_ssh_remote(tmp_path: Path) -> None:
    repo = resolve_github_repo(
        tmp_path, run=_fake_run(0, stdout="git@github.com:owner/name.git\n")
    )

    assert repo == "owner/name"


def test_resolve_github_repo_falls_back_to_gh_when_remote_missing(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:2] == ["git", "remote"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="no such remote")
        return subprocess.CompletedProcess(args, 0, stdout="owner/name\n", stderr="")

    repo = resolve_github_repo(tmp_path, run=_run)

    assert repo == "owner/name"
    assert any(args[0] == "gh" for args in calls)


def test_resolve_github_repo_returns_none_when_both_fail(tmp_path: Path) -> None:
    repo = resolve_github_repo(tmp_path, run=_fake_run(1, stdout="", stderr="error"))

    assert repo is None


def test_resolve_github_repo_rejects_non_github_remote(tmp_path: Path) -> None:
    repo = resolve_github_repo(
        tmp_path, run=_fake_run(0, stdout="https://gitlab.com/owner/name.git\n")
    )

    assert repo is None


def test_resolve_github_repo_returns_none_on_oserror(tmp_path: Path) -> None:
    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise OSError("git not found")

    assert resolve_github_repo(tmp_path, run=_run) is None


# --- Template rendering -------------------------------------------------------------


def test_render_subsched_yaml_with_repo_is_valid_config(tmp_path: Path) -> None:
    content = render_subsched_yaml(repo="owner/name", verification_commands=("pytest",))
    config_path = tmp_path / "subsched.yaml"
    config_path.write_text(content, encoding="utf-8")

    cfg = load_config(config_path)

    assert cfg.github.repo == "owner/name"
    assert cfg.verification.commands == ("pytest",)
    assert cfg.billing.api_fallback is False


def test_render_subsched_yaml_without_repo_comments_out_repo_key() -> None:
    content = render_subsched_yaml(repo=None, verification_commands=("pytest",))

    parsed = yaml.safe_load(content)

    assert parsed["github"].get("repo") is None
    assert "# repo: owner/name" in content


def test_render_subsched_yaml_without_repo_still_parses_as_valid_yaml(tmp_path: Path) -> None:
    content = render_subsched_yaml(repo=None, verification_commands=("pytest", "ruff check ."))
    config_path = tmp_path / "subsched.yaml"
    config_path.write_text(content, encoding="utf-8")

    cfg = load_config(config_path)

    assert cfg.github.repo is None


def test_render_agent_instructions_contains_required_guidelines() -> None:
    content = render_agent_instructions(verification_commands=("pytest",))

    assert "Simplicity First" in content
    assert "Surgical Changes" in content
    assert "Test-Driven Development" in content
    assert "auto-close keywords" in content
    assert "Closes #N" in content
    assert "`pytest`" in content


# --- Scaffold plan / write -----------------------------------------------------------


def test_build_scaffold_plan_includes_all_files_by_default(tmp_path: Path) -> None:
    plan = build_scaffold_plan(
        tmp_path,
        repo_override="owner/name",
        include_agents_md=True,
        include_claude_md=True,
        run=_fake_run(1),
    )

    names = {f.path.name for f in plan.files}
    assert names == {"subsched.yaml", "AGENTS.md", "CLAUDE.md"}
    assert plan.repo == "owner/name"


def test_build_scaffold_plan_respects_agents_and_claude_md_toggles(tmp_path: Path) -> None:
    plan = build_scaffold_plan(
        tmp_path,
        repo_override="owner/name",
        include_agents_md=False,
        include_claude_md=False,
        run=_fake_run(1),
    )

    names = {f.path.name for f in plan.files}
    assert names == {"subsched.yaml"}


def test_build_scaffold_plan_marks_existing_files(tmp_path: Path) -> None:
    (tmp_path / "subsched.yaml").write_text("existing")

    plan = build_scaffold_plan(
        tmp_path,
        repo_override="owner/name",
        include_agents_md=True,
        include_claude_md=True,
        run=_fake_run(1),
    )

    by_name = {f.path.name: f for f in plan.files}
    assert by_name["subsched.yaml"].exists is True
    assert by_name["AGENTS.md"].exists is False


def test_write_scaffold_plan_writes_all_files(tmp_path: Path) -> None:
    plan = build_scaffold_plan(
        tmp_path,
        repo_override="owner/name",
        include_agents_md=True,
        include_claude_md=True,
        run=_fake_run(1),
    )

    written = write_scaffold_plan(plan, force=False)

    assert set(written) == {
        tmp_path / "subsched.yaml",
        tmp_path / "AGENTS.md",
        tmp_path / "CLAUDE.md",
    }
    assert (tmp_path / "subsched.yaml").exists()
    assert (tmp_path / "AGENTS.md").read_text() == (tmp_path / "CLAUDE.md").read_text()


def test_write_scaffold_plan_refuses_overwrite_without_force(tmp_path: Path) -> None:
    (tmp_path / "subsched.yaml").write_text("existing content")
    plan = build_scaffold_plan(
        tmp_path,
        repo_override="owner/name",
        include_agents_md=False,
        include_claude_md=False,
        run=_fake_run(1),
    )

    with pytest.raises(InitError):
        write_scaffold_plan(plan, force=False)

    # Fail closed: refusing must not have touched the existing file.
    assert (tmp_path / "subsched.yaml").read_text() == "existing content"


def test_write_scaffold_plan_overwrites_with_force(tmp_path: Path) -> None:
    (tmp_path / "subsched.yaml").write_text("existing content")
    plan = build_scaffold_plan(
        tmp_path,
        repo_override="owner/name",
        include_agents_md=False,
        include_claude_md=False,
        run=_fake_run(1),
    )

    write_scaffold_plan(plan, force=True)

    assert (tmp_path / "subsched.yaml").read_text() != "existing content"
