"""Detached dispatch lifecycle can be queried without exposing child output."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from subsched.cli import app
from subsched.dispatch_runs import DispatchRunStore


def test_dispatch_status_json_reads_run_without_changing_it(tmp_path: Path) -> None:
    store = DispatchRunStore(tmp_path / ".ai" / "runtime")
    run_id = store.create()
    before = store._path(run_id).read_bytes()

    result = CliRunner().invoke(
        app, ["--repository", str(tmp_path), "dispatch-status", run_id, "--json"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "accepted"
    assert store._path(run_id).read_bytes() == before
