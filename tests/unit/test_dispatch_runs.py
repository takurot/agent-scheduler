"""Durable, read-only lifecycle status for detached MCP runs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from subsched.dispatch_runs import DispatchRunStore, run_child


def test_child_success_and_failure_are_separate_runs(tmp_path: Path) -> None:
    store = DispatchRunStore(tmp_path)
    first = store.create()
    second = store.create()

    assert run_child(store, first, [sys.executable, "-c", "print('secret raw output')"]) == 0
    assert run_child(store, second, [sys.executable, "-c", "raise SystemExit(7)"]) == 7
    assert store.inspect(first)["status"] == "succeeded"
    assert store.inspect(second)["status"] == "failed"
    assert store.inspect(second)["exit_code"] == 7
    assert DispatchRunStore(tmp_path).inspect(first)["status"] == "succeeded"
    assert "secret raw output" not in str(store.inspect(first))
    assert (tmp_path / "dispatch-runs" / f"{first}.log").stat().st_mode & 0o077 == 0


def test_config_failure_is_classified_without_exposing_output(tmp_path: Path) -> None:
    store = DispatchRunStore(tmp_path)
    run_id = store.create()

    assert run_child(
        store,
        run_id,
        [sys.executable, "-c", "print('config error: private-credential'); raise SystemExit(2)"],
    ) == 2
    record = store.inspect(run_id)
    assert record["reason"] == "config_error"
    assert "private-credential" not in str(record)
    assert "private-credential" not in (tmp_path / "dispatch-runs" / f"{run_id}.log").read_text()


def test_pid_reuse_is_reported_stale(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = DispatchRunStore(tmp_path)
    run_id = store.create()
    store.update(run_id, status="running", pid=123, process_start_time="old")
    monkeypatch.setattr("subsched.dispatch_runs.get_process_start_time", lambda pid: "new")

    assert store.inspect(run_id)["status"] == "stale"


def test_detached_entrypoint_survives_reader_restart(tmp_path: Path) -> None:
    runtime = tmp_path / ".ai" / "runtime"
    store = DispatchRunStore(runtime)
    run_id = store.create()

    result = subprocess.run(
        [
            sys.executable, "-m", "subsched.dispatch_runs", str(tmp_path), run_id,
            sys.executable, "-c", "raise SystemExit(0)",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert DispatchRunStore(runtime).inspect(run_id)["status"] == "succeeded"


def test_invalid_run_id_never_reads_arbitrary_path(tmp_path: Path) -> None:
    store = DispatchRunStore(tmp_path)
    with pytest.raises(ValueError, match="run_id"):
        store.inspect("../scheduler.json")


def test_symlinked_runtime_directory_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    runtime_link = tmp_path / "runtime"
    runtime_link.symlink_to(real)
    store = DispatchRunStore(runtime_link)

    with pytest.raises(ValueError, match="symlink"):
        store.inspect("a" * 24)


def test_lifecycle_log_rotates_without_exposing_child_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("subsched.dispatch_runs._MAX_LOG_BYTES", 100)
    store = DispatchRunStore(tmp_path)
    run_id = store.create()
    for _ in range(4):
        store._log(run_id, "running")

    log = tmp_path / "dispatch-runs" / f"{run_id}.log"
    assert log.stat().st_size <= 100
    assert log.with_name(log.name + ".1").exists()
