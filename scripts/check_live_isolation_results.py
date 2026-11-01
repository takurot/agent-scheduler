"""Validate the required live-isolation pytest JUnit result, failing closed."""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REQUIRED = {
    "test_real_container_blocks_host_credentials_and_imports_task_commit",
    "test_real_container_timeout_cleanup_kills_descendants",
    "test_real_container_cannot_rewrite_linked_worktree_git_pointer",
    "test_real_container_reproduces_and_resolves_codex_bwrap_sandbox_conflict",
}
CLASSNAME = "tests.integration.test_native_isolation_container"


def passed(path: Path) -> bool:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return False
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if root.tag not in {"testsuite", "testsuites"} or len(suites) != 1:
        return False
    suite = suites[0]
    if any(
        suite.get(key) != value
        for key, value in {"tests": "4", "failures": "0", "errors": "0", "skipped": "0"}.items()
    ):
        return False
    cases = suite.findall("testcase")
    return (
        len(cases) == len(REQUIRED)
        and {case.get("name") for case in cases} == REQUIRED
        and all(case.get("classname") == CLASSNAME for case in cases)
        and all(case.find(tag) is None for case in cases for tag in ("skipped", "failure", "error"))
    )


if __name__ == "__main__":
    if len(sys.argv) != 2 or not passed(Path(sys.argv[1])):
        print(
            "error: all four required live isolation tests must pass without skips", file=sys.stderr
        )
        sys.exit(1)
