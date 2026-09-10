from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from subsched.cli import app
from subsched.config import ConfigError, load_config, parse_duration

runner = CliRunner()


def test_integer_max_task_runtime_round_trip_through_load_config_and_parse_duration(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
execution:
  max_task_runtime: 3600
""",
        encoding="utf-8",
    )

    cfg = load_config(config_file)
    assert cfg.execution.max_task_runtime == 3600
    # Must parse successfully without error
    parsed = parse_duration(cfg.execution.max_task_runtime)
    assert parsed == 3600


def test_string_with_units_max_task_runtime(tmp_path: Path) -> None:
    for val, expected in (("3600s", 3600), ("6h", 21600), ("30m", 1800), ("1d", 86400)):
        config_file = tmp_path / "scheduler.yaml"
        config_file.write_text(
            f"""
github:
  repo: owner/project
execution:
  max_task_runtime: {val}
""",
            encoding="utf-8",
        )
        cfg = load_config(config_file)
        assert parse_duration(cfg.execution.max_task_runtime) == expected


def test_unitless_string_max_task_runtime_fails_validation(tmp_path: Path) -> None:
    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
execution:
  max_task_runtime: "3600"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="invalid duration format"):
        load_config(config_file)


def test_cli_config_validate_and_run_accept_integer_max_task_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.github.issues import GitHubIssueSource

    monkeypatch.setattr(GitHubIssueSource, "list_open", lambda self, repo, **kwargs: ())

    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        """
github:
  repo: owner/project
execution:
  max_task_runtime: 3600
""",
        encoding="utf-8",
    )

    # config validate succeeds
    val_res = runner.invoke(app, ["config", "validate", "--config", str(config_file)])
    assert val_res.exit_code == 0, val_res.output
    assert "valid" in val_res.output.lower()

    # run --dry-run succeeds
    run_res = runner.invoke(
        app,
        ["--repository", str(tmp_path), "run", "--config", str(config_file), "--dry-run"],
    )
    assert run_res.exit_code == 0, run_res.output
