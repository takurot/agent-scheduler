from __future__ import annotations

import importlib.metadata
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

import subsched
from subsched.cli import app

_PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_public_version_matches_distribution_metadata() -> None:
    assert subsched.__version__ == importlib.metadata.version("agent-scheduler")


def test_public_version_uses_installed_metadata_after_version_bump() -> None:
    with patch("importlib.metadata.version", return_value="1.2.3") as version:
        namespace = runpy.run_path(str(_PYPROJECT_PATH.parent / "src/subsched/__init__.py"))
    assert namespace["__version__"] == "1.2.3"
    version.assert_called_once_with("agent-scheduler")


def test_pyproject_declares_both_console_scripts() -> None:
    data = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert scripts["subsched"] == "subsched.cli:app"
    assert scripts["agent-scheduler"] == "subsched.cli:app"


def test_agent_scheduler_entry_point_registered() -> None:
    entry_points = importlib.metadata.entry_points(group="console_scripts")
    names = {ep.name for ep in entry_points}
    assert "agent-scheduler" in names
    assert "subsched" in names


def test_logging_module_does_not_shadow_stdlib() -> None:
    # subsched.logging previously shadowed the stdlib `logging` module when
    # the package directory itself was placed on sys.path (e.g. running a
    # script directly from within src/subsched/). Renaming the module removes
    # the entire class of bug; this test guards against reintroducing a
    # module named `logging` inside the subsched package.
    package_dir = Path(__file__).resolve().parents[2] / "src" / "subsched"
    assert not (package_dir / "logging.py").exists()


def test_python_module_entrypoint() -> None:
    res = subprocess.run(
        [sys.executable, "-m", "subsched", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "Subscription-aware coding agent scheduler" in res.stdout


def test_main_module_execution() -> None:
    with patch("subsched.cli.app") as mock_app:
        runpy.run_module("subsched.__main__", run_name="__main__")
        mock_app.assert_called_once()


def test_cli_app_commands_registered() -> None:
    command_names = {
        c.name or (c.callback.__name__ if c.callback else None)
        for c in app.registered_commands
    }
    expected = {"run", "status", "pause", "resume", "cancel", "doctor", "metrics"}
    assert expected.issubset(command_names)
