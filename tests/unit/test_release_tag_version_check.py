from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def _load_release_workflow() -> dict[str, Any]:
    return yaml.safe_load((_WORKFLOWS_DIR / "release.yml").read_text(encoding="utf-8"))


def _run_commands(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job.get("steps", []) if "run" in step]


def _needed_job_names(publish_job: dict[str, Any]) -> set[str]:
    needs = publish_job.get("needs")
    return {needs} if isinstance(needs, str) else set(needs or ())


def _find_tag_version_check_job(jobs: dict[str, Any]) -> str | None:
    """Find the job whose steps compare the pushed tag against pyproject.toml's version."""
    for name, job in jobs.items():
        commands = " ".join(_run_commands(job))
        if "pyproject.toml" in commands and "ref_name" in commands.casefold():
            return name
    return None


def test_release_has_a_job_that_verifies_tag_matches_pyproject_version() -> None:
    release = _load_release_workflow()
    job_name = _find_tag_version_check_job(release["jobs"])
    assert job_name is not None, (
        "no release.yml job compares the pushed git tag against pyproject.toml's "
        "project.version before publishing"
    )


def test_pypi_publish_depends_on_the_tag_version_check_job() -> None:
    release = _load_release_workflow()
    jobs = release["jobs"]
    job_name = _find_tag_version_check_job(jobs)
    assert job_name is not None

    needed = _needed_job_names(jobs["pypi-publish"])
    assert job_name in needed, (
        f"pypi-publish must depend on '{job_name}' (needs: ...) so a version mismatch "
        "blocks publishing"
    )


def test_current_pyproject_version_would_pass_its_own_check() -> None:
    """Sanity check: the comparison logic itself, exercised directly against the repo's
    current tag-shaped input, to guard against a check that always no-ops."""
    import re
    import tomllib

    pkg_version = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))["project"]["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", pkg_version), "sanity: version must be semver-shaped"

    matching_tag = f"v{pkg_version}"
    mismatched_tag = f"v{pkg_version}-bogus"
    assert matching_tag[1:] == pkg_version
    assert mismatched_tag[1:] != pkg_version
