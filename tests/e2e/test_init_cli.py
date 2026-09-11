"""E2E tests for `subsched init` (#258) through the actual Typer CLI."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from subsched.cli import app
from subsched.config import load_config

runner = CliRunner()


def test_init_scaffolds_all_files_with_explicit_repo(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path), "--repo", "owner/name"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "subsched.yaml").exists()
    assert (tmp_path / "AGENTS.md").exists()
    assert (tmp_path / "CLAUDE.md").exists()

    cfg = load_config(tmp_path / "subsched.yaml")
    assert cfg.github.repo == "owner/name"


def test_init_detects_python_stack_and_writes_pytest_commands(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")

    result = runner.invoke(app, ["init", str(tmp_path), "--repo", "owner/name"])

    assert result.exit_code == 0, result.output
    assert "Detected stack: python" in result.output
    cfg = load_config(tmp_path / "subsched.yaml")
    assert cfg.verification.commands == ("pytest", "ruff check .")


def test_init_respects_no_agents_md_and_no_claude_md_flags(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["init", str(tmp_path), "--repo", "owner/name", "--no-agents-md", "--no-claude-md"],
    )

    assert result.exit_code == 0, result.output
    assert (tmp_path / "subsched.yaml").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_init_dry_run_previews_without_writing(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path), "--repo", "owner/name", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Would create" in result.output
    assert not (tmp_path / "subsched.yaml").exists()
    assert not (tmp_path / "AGENTS.md").exists()
    assert not (tmp_path / "CLAUDE.md").exists()


def test_init_refuses_overwrite_without_force(tmp_path: Path) -> None:
    (tmp_path / "subsched.yaml").write_text("existing", encoding="utf-8")

    result = runner.invoke(app, ["init", str(tmp_path), "--repo", "owner/name"])

    assert result.exit_code != 0
    assert (tmp_path / "subsched.yaml").read_text(encoding="utf-8") == "existing"


def test_init_overwrites_with_force(tmp_path: Path) -> None:
    (tmp_path / "subsched.yaml").write_text("existing", encoding="utf-8")

    result = runner.invoke(app, ["init", str(tmp_path), "--repo", "owner/name", "--force"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "subsched.yaml").read_text(encoding="utf-8") != "existing"


def test_init_rejects_invalid_repo_option(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path), "--repo", "not-a-valid-repo"])

    assert result.exit_code != 0


def test_init_rejects_non_directory_path(tmp_path: Path) -> None:
    file_path = tmp_path / "not-a-dir"
    file_path.write_text("x", encoding="utf-8")

    result = runner.invoke(app, ["init", str(file_path), "--repo", "owner/name"])

    assert result.exit_code != 0


def test_init_warns_when_repo_cannot_be_detected(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "could not auto-detect" in result.output.lower()
    cfg = load_config(tmp_path / "subsched.yaml")
    assert cfg.github.repo is None
