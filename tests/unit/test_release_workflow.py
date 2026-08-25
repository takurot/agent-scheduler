from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _load_workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOWS_DIR / name).read_text(encoding="utf-8"))


def _action_versions(job: dict[str, Any]) -> dict[str, str]:
    """Map action name (without version) -> pinned version, e.g. {'actions/checkout': 'v7'}."""
    versions: dict[str, str] = {}
    for step in job.get("steps", []):
        uses = step.get("uses")
        if not uses or "@" not in uses:
            continue
        name, _, version = uses.partition("@")
        versions[name] = version
    return versions


def _run_commands(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def test_release_publish_job_requires_the_quality_gate_job() -> None:
    release = _load_workflow("release.yml")
    jobs = release["jobs"]
    assert "pypi-publish" in jobs

    publish_job = jobs["pypi-publish"]
    needs = publish_job.get("needs")
    needed = {needs} if isinstance(needs, str) else set(needs or ())
    assert needed, "pypi-publish must depend on a quality-gate job (needs: ...)"

    gate_job_name = next(iter(needed))
    assert gate_job_name in jobs, f"quality gate job '{gate_job_name}' is not defined"


def test_release_quality_gate_runs_the_same_checks_as_ci() -> None:
    ci = _load_workflow("ci.yml")
    release = _load_workflow("release.yml")

    ci_commands = _run_commands(ci["jobs"]["verify"])

    gate_job_name = release["jobs"]["pypi-publish"]["needs"]
    if not isinstance(gate_job_name, str):
        gate_job_name = next(iter(gate_job_name))
    gate_commands = _run_commands(release["jobs"][gate_job_name])

    # Every quality-gate command in CI (lint, typecheck, test+coverage, dependency audit)
    # must also run before a release is published.
    required_substrings = ("ruff check", "mypy", "pytest", "pip-audit")
    for substring in required_substrings:
        assert any(substring in cmd for cmd in ci_commands), f"CI is missing a '{substring}' step"
        assert any(
            substring in cmd for cmd in gate_commands
        ), f"release quality gate is missing a '{substring}' step"


def test_release_and_ci_pin_the_same_action_versions() -> None:
    ci = _load_workflow("ci.yml")
    release = _load_workflow("release.yml")

    ci_versions = _action_versions(ci["jobs"]["verify"])

    gate_job_name = release["jobs"]["pypi-publish"]["needs"]
    if not isinstance(gate_job_name, str):
        gate_job_name = next(iter(gate_job_name))

    for job in release["jobs"].values():
        job_versions = _action_versions(job)
        for action, ci_version in ci_versions.items():
            if action in job_versions:
                assert job_versions[action] == ci_version, (
                    f"{action} is pinned to {job_versions[action]} in release.yml but "
                    f"{ci_version} in ci.yml"
                )


def test_release_triggers_only_on_semver_tags() -> None:
    release = _load_workflow("release.yml")
    # PyYAML's safe_load parses the bare `on:` trigger key as the boolean True (YAML 1.1).
    tags = release[True]["push"]["tags"]
    assert any(re.fullmatch(r"v\*\.\*\.\*", pattern) for pattern in tags)
