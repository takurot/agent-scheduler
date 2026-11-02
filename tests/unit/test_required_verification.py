from pathlib import Path

import pytest
import yaml

from subsched.config import ConfigError, load_config


@pytest.mark.parametrize(
    "commands,required,valid",
    [
        (["bash scripts/quality_gate.sh"], ["bash scripts/quality_gate.sh"], True),
        (["pytest tests/unit"], ["pytest"], False),
        (["python -c 'print(\"pytest\")'"], ["pytest"], False),
        (["pytest", "ruff check ."], ["pytest", "ruff check ."], True),
        (["python -c pass"], None, True),
        (["python -c pass"], [], False),
        (["python -c pass"], [" "], False),
        (["python -c pass"], "pytest", False),
        (["python -c pass"], [True], False),
        (["python -c pass"], ["'"], False),
    ],
)
def test_required_commands_policy(
    tmp_path: Path,
    commands: list[str],
    required: object,
    valid: bool,
) -> None:
    verification = {"commands": commands}
    if required is not None:
        verification["required_commands"] = required
    path = tmp_path / "subsched.yaml"
    path.write_text(yaml.safe_dump({"verification": verification}), encoding="utf-8")
    if valid:
        config = load_config(path)
        assert config.verification.required_commands == tuple(required or ())
    else:
        with pytest.raises(ConfigError, match="required_commands"):
            load_config(path)


def test_required_policy_is_visible_and_failed_gate_cannot_pass(tmp_path: Path) -> None:
    import hashlib
    import sys

    from typer.testing import CliRunner

    from subsched.cli import app
    from subsched.verification import run_verification

    command = f'{sys.executable} -c "raise SystemExit(17)"'
    path = tmp_path / "subsched.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "github": {"repo": "owner/name"},
                "verification": {
                    "commands": [command],
                    "required_commands": [command],
                },
            }
        ),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app, ["--repository", str(tmp_path), "config", "validate", "--dry-run"]
    )
    fingerprint = hashlib.sha256(command.encode()).hexdigest()
    assert result.exit_code == 0, result.output
    assert "required policy: satisfied" in result.output
    assert fingerprint in result.output
    report = run_verification(tmp_path, load_config(path).verification.commands)
    assert report.passed is False
    assert fingerprint in report.summary
    assert "FAIL (exit 17)" in report.summary
