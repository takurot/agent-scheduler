"""Operator-run Docker isolation check must fail closed before touching Docker."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live-isolation-check.sh"


def test_missing_images_fail_before_docker_is_called(tmp_path: Path) -> None:
    fake_docker = tmp_path / "docker"
    calls = tmp_path / "docker-called"
    fake_docker.write_text(f"#!/bin/sh\ntouch '{calls}'\nexit 0\n", encoding="utf-8")
    fake_docker.chmod(0o755)
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "SUBSCHED_ISOLATION_REPORT": str(tmp_path / "report.json"),
    }

    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=10
    )

    assert result.returncode != 0
    assert "SUBSCHED_ISOLATION_WORKER_IMAGE" in result.stderr
    assert not calls.exists()
    assert not (tmp_path / "report.json").exists()


def test_warning_summary_with_four_passing_junit_cases_succeeds(tmp_path: Path) -> None:
    from test_live_isolation_results import TESTS

    image = "test/image@sha256:" + "a" * 64
    xml = tmp_path / "results.xml"
    cases = "".join(
        f'<testcase classname="tests.integration.test_native_isolation_container" name="{name}"/>'
        for name in TESTS
    )
    xml.write_text(
        f'<testsuite tests="4" failures="0" errors="0" skipped="0">{cases}</testsuite>',
        encoding="utf-8",
    )
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "image inspect") printf \'["%s"]\\n\' "$3" ;;\n'
        '  "info --format") echo test-version ;;\n'
        '  "network create") echo test-network ;;\n'
        '  "run -d") echo test-proxy ;;\n'
        "esac\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = sync ]; then exit 0; fi\n'
        "shift\n"
        'if [ "$1" = --no-sync ]; then shift; exec "$@"; fi\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in --junitxml=*) cp "$TEST_XML" "${arg#--junitxml=}" ;; esac\n'
        "done\n"
        'echo "4 passed, 1 warning in 0.10s"\n',
        encoding="utf-8",
    )
    uv.chmod(0o755)
    report = tmp_path / "report.json"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "SUBSCHED_ISOLATION_WORKER_IMAGE": image,
        "SUBSCHED_ISOLATION_PROXY_IMAGE": image,
        "SUBSCHED_ISOLATION_REPORT": str(report),
        "TEST_XML": str(xml),
    }
    result = subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert "4 passed, 1 warning" in result.stdout
    assert json.loads(report.read_text(encoding="utf-8"))["result"] == "passed"
