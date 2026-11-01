from __future__ import annotations

import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "scripts" / "check_live_isolation_results.py"
TESTS = (
    "test_real_container_blocks_host_credentials_and_imports_task_commit",
    "test_real_container_timeout_cleanup_kills_descendants",
    "test_real_container_cannot_rewrite_linked_worktree_git_pointer",
    "test_real_container_reproduces_and_resolves_codex_bwrap_sandbox_conflict",
)


@pytest.mark.parametrize(
    ("problem", "expected"),
    [
        ("none", 0),
        ("warning", 0),
        ("skipped", 1),
        ("failure", 1),
        ("error", 1),
        ("missing", 1),
        ("duplicate", 1),
        ("wrong_class", 1),
        ("bad_xml", 1),
        ("missing_file", 1),
    ],
)
def test_classifies_required_junit_results(tmp_path: Path, problem: str, expected: int) -> None:
    result_file = tmp_path / "results.xml"
    suite = ET.Element("testsuite", tests="4", failures="0", errors="0", skipped="0")
    for name in TESTS:
        ET.SubElement(
            suite,
            "testcase",
            classname="tests.integration.test_native_isolation_container",
            name=name,
        )
    cases = list(suite)
    if problem in {"skipped", "failure", "error"}:
        ET.SubElement(cases[0], problem)
    elif problem == "warning":
        ET.SubElement(cases[0], "system-err").text = "1 warning"
    elif problem == "missing":
        suite.remove(cases[0])
    elif problem == "duplicate":
        cases[0].set("name", TESTS[1])
    elif problem == "wrong_class":
        cases[0].set("classname", "unrelated")
    if problem != "missing_file":
        if problem == "bad_xml":
            result_file.write_text("<broken", encoding="utf-8")
        else:
            ET.ElementTree(suite).write(result_file, encoding="utf-8", xml_declaration=True)
    result = subprocess.run([sys.executable, str(VALIDATOR), str(result_file)], capture_output=True)
    assert result.returncode == expected
