from __future__ import annotations

from pathlib import Path

import pytest

from subsched.config import ConfigError, WorkflowConfig, load_config


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "scheduler.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_workflow_defaults_to_standard_when_omitted(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, "github:\n  repo: o/r\n"))
    assert config.workflow == WorkflowConfig()
    assert config.workflow.mode == "standard"
    assert config.workflow.stages.planning is True
    assert config.workflow.stages.plan_review is True
    assert config.workflow.limits.max_plan_revisions == 2


def test_workflow_multi_stage_accepted(tmp_path: Path) -> None:
    config = load_config(
        _write(
            tmp_path,
            "github:\n  repo: o/r\n"
            "workflow:\n  mode: multi-stage\n  stages:\n    planning: true\n"
            "    plan_review: false\n  limits:\n    max_plan_revisions: 5\n",
        )
    )
    assert config.workflow.mode == "multi-stage"
    assert config.workflow.stages.planning is True
    assert config.workflow.stages.plan_review is False
    assert config.workflow.limits.max_plan_revisions == 5


def test_workflow_rejects_unsupported_mode(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unsupported workflow\.mode"):
        load_config(_write(tmp_path, "github:\n  repo: o/r\nworkflow:\n  mode: yolo\n"))


def test_workflow_rejects_unknown_root_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unknown workflow\.stages keys"):
        load_config(
            _write(
                tmp_path,
                "github:\n  repo: o/r\nworkflow:\n  stages:\n    bogus: true\n",
            )
        )


def test_workflow_rejects_unknown_limits_key(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"unknown workflow\.limits keys"):
        load_config(
            _write(
                tmp_path,
                "github:\n  repo: o/r\nworkflow:\n  limits:\n    bogus: 1\n",
            )
        )


def test_workflow_rejects_non_positive_max_plan_revisions(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="positive integer"):
        load_config(
            _write(
                tmp_path,
                "github:\n  repo: o/r\nworkflow:\n  limits:\n    max_plan_revisions: 0\n",
            )
        )


def test_workflow_rejects_non_boolean_stage_flag(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"workflow\.stages\.planning"):
        load_config(
            _write(
                tmp_path,
                "github:\n  repo: o/r\nworkflow:\n  stages:\n    planning: yes-please\n",
            )
        )
