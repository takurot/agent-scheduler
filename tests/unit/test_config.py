from pathlib import Path

import pytest

from subsched.config import ConfigError, load_config


def test_config_loads_safe_defaults(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text("github:\n  repo: owner/project\n", encoding="utf-8")

    config = load_config(path)

    assert config.github.repo == "owner/project"
    assert config.execution.concurrency == 1
    assert config.billing.metered_usage is False
    assert config.billing.unknown_mode == "disable"


def test_config_rejects_parallel_execution_in_phase1(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text("github:\n  repo: o/r\nexecution:\n  concurrency: 2\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="concurrency"):
        load_config(path)


def test_config_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "scheduler.yaml"
    path.write_text("github:\n  repo: o/r\nunknown: true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="unknown"):
        load_config(path)
