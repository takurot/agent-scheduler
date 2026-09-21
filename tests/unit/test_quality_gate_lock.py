"""The local quality gate must not repair a stale lockfile while validating it."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "quality_gate.sh"


def test_stale_lock_fails_without_rewriting_it(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(SCRIPT, scripts / "quality_gate.sh")
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\nname = "lock-check-fixture"\nversion = "0.1.0"\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )
    subprocess.run(["uv", "lock", "--offline"], cwd=tmp_path, check=True, capture_output=True)
    lock = tmp_path / "uv.lock"
    original = lock.read_bytes()
    pyproject.write_text(
        '[project]\nname = "lock-check-fixture"\nversion = "0.2.0"\nrequires-python = ">=3.12"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(scripts / "quality_gate.sh")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert lock.read_bytes() == original
