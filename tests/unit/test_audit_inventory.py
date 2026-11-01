from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
import venv
from datetime import UTC, datetime
from pathlib import Path

import msgpack
import pytest

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.audit_inventory import (
    canonical_inventory,
    expected_cache_paths,
    inspect_audit_result,
    produce_audit_record,
    run_data_audit,
)


def test_inventory_is_normalized_sorted_and_deterministic() -> None:
    assert canonical_inventory([("Z.foo", "1.2"), ("alpha", "2.0")]) == (
        b'[{"name":"alpha","version":"2.0"},{"name":"z-foo","version":"1.2"}]'
    )


def test_inventory_module_import_does_not_require_optional_audit_dependencies() -> None:
    script = (
        "import builtins; original=builtins.__import__; "
        "exec('def guarded(name,*args,**kwargs):\\n"
        " if name == \\\"msgpack\\\": raise ImportError(\\\"not installed\\\")\\n"
        " return original(name,*args,**kwargs)'); "
        "builtins.__import__=guarded; import subsched.audit_inventory"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_expected_cache_keys_bind_canonical_package_and_version() -> None:
    digest = hashlib.sha224(b"https://pypi.org/pypi/packaging/26.3/json").hexdigest()
    assert expected_cache_paths([("Packaging", "26.3")]) == {"/".join([*digest[:5], digest])}


@pytest.mark.parametrize(
    "items", [[], [("bad/name", "1")], [("safe", "1;bad")], [("A_b", "1"), ("a-b", "2")]]
)
def test_inventory_rejects_invalid_or_duplicate_entries(items: list[tuple[str, str]]) -> None:
    with pytest.raises(ValueError):
        canonical_inventory(items)


def result(code: int, dependencies: list[dict]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=code,
        stdout=json.dumps({"dependencies": dependencies, "fixes": []}).encode(),
        stderr=b"",
    )


def cached_pypi_response(payload: object) -> bytes:
    body = json.dumps(payload).encode()
    return b"cc=4," + msgpack.packb({"response": {
        "status": 200, "headers": {}, "body": body,
    }, "vary": {}}, use_bin_type=True)


def test_clean_requires_exact_inventory_and_clean_exit() -> None:
    data = [{"name": "Demo_pkg", "version": "1.0", "vulns": []}]
    assert inspect_audit_result([("demo-pkg", "1.0")], result(0, data)) == "clean"
    assert (
        inspect_audit_result([("demo-pkg", "1.0"), ("missing", "2")], result(0, data))
        == "unavailable"
    )
    assert inspect_audit_result([("demo-pkg", "1.0")], result(2, data)) == "unavailable"


def test_advisory_is_not_a_clean_result() -> None:
    data = [{"name": "demo", "version": "1", "vulns": [{"id": "PYSEC-2026-1"}]}]
    assert inspect_audit_result([("demo", "1")], result(1, data)) == "advisory"


def test_unexpected_fix_action_cannot_be_clean() -> None:
    process = result(0, [{"name": "demo", "version": "1", "vulns": []}])
    report = json.loads(process.stdout)
    report["fixes"] = [{"name": "demo"}]
    altered = subprocess.CompletedProcess(args=[], returncode=0, stdout=json.dumps(report).encode())
    assert inspect_audit_result([("demo", "1")], altered) == "unavailable"


@pytest.mark.parametrize("stdout", [b"", b"{}", b'{"dependencies":[]}', b"not json"])
def test_malformed_or_empty_report_is_unavailable(stdout: bytes) -> None:
    process = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=b"")
    assert inspect_audit_result([("demo", "1")], process) == "unavailable"


def test_data_only_audit_uses_isolated_cwd_and_fixed_argv(tmp_path: Path) -> None:
    tool = tmp_path / "pip-audit"
    tool.touch()
    requests: list[ProcessExecutionRequest] = []

    def fake_runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        requests.append(request)
        assert request.cwd != tmp_path
        assert request.env == {"HOME": str(request.cwd), "PATH": "/usr/bin:/bin"}
        if request.argv[-1] == "--version":
            return ProcessExecutionResult(exit_code=0, stdout="pip-audit 2.10.1\n")
        assert "--disable-pip" in request.argv and "--no-deps" in request.argv
        requirements = Path(request.argv[-1])
        assert requirements.parent == request.cwd
        assert requirements.read_text() == "demo==1\n"
        return ProcessExecutionResult(
            exit_code=0,
            stdout='{"dependencies":[{"name":"demo","version":"1","vulns":[]}],"fixes":[]}',
        )

    assert run_data_audit([("demo", "1")], tool, "pip-audit 2.10.1", runner=fake_runner) == "clean"
    assert len(requests) == 2
    assert not requests[0].cwd.exists()


@pytest.mark.parametrize("populate", [False, True])
def test_clean_audit_requires_complete_retained_cache(tmp_path: Path, populate: bool) -> None:
    tool = tmp_path / "pip-audit"
    tool.touch()
    cache = tmp_path / "cache"
    cache.mkdir()

    def fake_runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        if request.argv[-1] == "--version":
            return ProcessExecutionResult(exit_code=0, stdout="pip-audit 2.10.1\n")
        if populate:
            entry = cache / next(iter(expected_cache_paths([("demo", "1")])))
            entry.parent.mkdir(parents=True)
            entry.write_bytes(cached_pypi_response({
                "info": {"name": "demo", "version": "1"}, "vulnerabilities": [],
            }))
        return ProcessExecutionResult(
            exit_code=0,
            stdout='{"dependencies":[{"name":"demo","version":"1","vulns":[]}],"fixes":[]}',
        )

    outcome = run_data_audit(
        [("demo", "1")], tool, "pip-audit 2.10.1", cache_dir=cache, runner=fake_runner
    )
    assert outcome == ("clean" if populate else "unavailable")


@pytest.mark.parametrize("payload", [
    {},
    {"info": {"name": "demo", "version": "1"}, "vulnerabilities": None},
    {"info": {"name": "other", "version": "1"}, "vulnerabilities": []},
    {"info": {"name": "demo", "version": "2"}, "vulnerabilities": []},
    {"info": {"name": "demo", "version": "1"}, "vulnerabilities": [{"id": "X"}]},
])
def test_malformed_cached_upstream_metadata_is_never_clean(
    tmp_path: Path, payload: object
) -> None:
    tool = tmp_path / "pip-audit"
    tool.touch()
    cache = tmp_path / "cache"
    cache.mkdir()

    def runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        if request.argv[-1] == "--version":
            return ProcessExecutionResult(exit_code=0, stdout="pip-audit 2.10.1\n")
        entry = cache / next(iter(expected_cache_paths([("demo", "1")])))
        entry.parent.mkdir(parents=True)
        entry.write_bytes(cached_pypi_response(payload))
        return ProcessExecutionResult(
            exit_code=0,
            stdout='{"dependencies":[{"name":"demo","version":"1","vulns":[]}],"fixes":[]}',
        )

    assert run_data_audit([("demo", "1")], tool, "pip-audit 2.10.1",
                          cache_dir=cache, runner=runner) == "unavailable"


def test_audit_launch_suppresses_python_startup_hooks(tmp_path: Path) -> None:
    tool = tmp_path / "pip-audit"
    tool.touch()
    requests: list[ProcessExecutionRequest] = []

    def runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        requests.append(request)
        if request.argv[-1] == "--version":
            return ProcessExecutionResult(exit_code=0, stdout="pip-audit 2.10.1\n")
        return ProcessExecutionResult(
            exit_code=0,
            stdout='{"dependencies":[{"name":"demo","version":"1","vulns":[]}],"fixes":[]}',
        )

    assert run_data_audit([("demo", "1")], tool, "pip-audit 2.10.1", runner=runner) == "clean"
    assert len(requests) == 2
    assert all(request.argv[0] == str(tmp_path / "python") for request in requests)
    assert all(request.argv[1:3] == ("-I", "-S") for request in requests)


def test_audit_and_nested_pip_do_not_execute_pth_hooks(tmp_path: Path) -> None:
    virtualenv = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(virtualenv)
    site = (
        virtualenv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    marker = tmp_path / "executed"
    (site / "canary.pth").write_text(f"import pathlib; pathlib.Path({str(marker)!r}).touch()\n")
    pip = site / "pip"
    pip.mkdir()
    (pip / "__init__.py").write_text("")
    (pip / "__main__.py").write_text("print('pip 1.0')\n")
    audit = site / "pip_audit"
    audit.mkdir()
    (audit / "__init__.py").write_text("")
    (audit / "__main__.py").write_text(
        "import os,subprocess,sys\n"
        "subprocess.check_output([os.environ['PIPAPI_PYTHON_LOCATION'],'-m','pip','--version'])\n"
        "assert subprocess.run([os.environ['PIPAPI_PYTHON_LOCATION'],'-m','pip','install',"
        "'evil'],check=False).returncode == 126\n"
        "print('pip-audit 2.10.1' if '--version' in sys.argv else "
        "'{\"dependencies\":[{\"name\":\"demo\",\"version\":\"1\",\"vulns\":[]}],\"fixes\":[]}')\n"
    )
    tool = virtualenv / "bin" / "pip-audit"
    tool.touch()
    assert run_data_audit([("demo", "1")], tool, "pip-audit 2.10.1") == "clean"
    assert not marker.exists()


def test_cached_response_gzip_size_is_bounded(tmp_path: Path) -> None:
    from subsched.audit_inventory import _valid_cached_response

    path = tmp_path / "cache"
    expected = {"name": "demo", "version": "1"}
    body = json.dumps({"info": expected, "vulnerabilities": []}).encode()
    def write(data: bytes) -> None:
        path.write_bytes(b"cc=4," + msgpack.packb({"response": {
            "status": 200, "headers": {"content-encoding": "gzip"}, "body": data,
        }, "vary": {}}, use_bin_type=True))
    write(gzip.compress(body))
    assert _valid_cached_response(path, expected)
    write(gzip.compress(b"x" * (1024 * 1024 + 1)))
    assert not _valid_cached_response(path, expected)


@pytest.mark.parametrize(
    "problem", ["version", "timeout", "output_limit", "cleanup", "command_not_found"]
)
def test_data_only_audit_rejects_tool_anomalies(tmp_path: Path, problem: str) -> None:
    tool = tmp_path / "pip-audit"
    tool.touch()

    def fake_runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        if problem == "version":
            return ProcessExecutionResult(exit_code=0, stdout="pip-audit 1.0\n")
        if request.argv[-1] == "--version":
            return ProcessExecutionResult(exit_code=0, stdout="pip-audit 2.10.1\n")
        return ProcessExecutionResult(
            exit_code=0,
            stdout='{"dependencies":[{"name":"demo","version":"1","vulns":[]}],"fixes":[]}',
            timed_out=problem == "timeout",
            output_limit_exceeded=problem == "output_limit",
            cleanup_succeeded=problem != "cleanup",
            command_not_found=problem == "command_not_found",
        )

    assert (
        run_data_audit([("demo", "1")], tool, "pip-audit 2.10.1", runner=fake_runner)
        == "unavailable"
    )


def test_producer_binds_record_to_exact_inventory_and_tool(tmp_path: Path) -> None:
    items = [("demo", "1")]
    target = {
        "repository": "owner/repo",
        "commit": "a" * 40,
        "lock_sha256": "b" * 64,
        "packages_sha256": hashlib.sha256(canonical_inventory(items)).hexdigest(),
        "tool": "pip-audit 2.10.1",
        "policy": "test",
        "workflow_sha256": "c" * 64,
    }
    tool = tmp_path / "pip-audit"
    tool.touch()

    def fake_runner(request: ProcessExecutionRequest) -> ProcessExecutionResult:
        if request.argv[-1] == "--version":
            return ProcessExecutionResult(exit_code=0, stdout="pip-audit 2.10.1\n")
        return ProcessExecutionResult(
            exit_code=0,
            stdout='{"dependencies":[{"name":"demo","version":"1","vulns":[]}],"fixes":[]}',
        )

    observed = datetime(2026, 11, 1, tzinfo=UTC)
    record = produce_audit_record(items, target, tool, observed, runner=fake_runner)
    assert record == {
        "schema": 1,
        "target": target,
        "observed_at": observed.isoformat(),
        "outcome": "clean",
    }
    bad_target = {**target, "packages_sha256": "f" * 64}
    with pytest.raises(ValueError):
        produce_audit_record(items, bad_target, tool, observed, runner=fake_runner)
