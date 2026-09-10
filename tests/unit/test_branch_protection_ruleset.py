from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RULESET_FILE = _REPO_ROOT / ".github" / "branch-protection" / "main.json"
_APPLY_SCRIPT = _REPO_ROOT / "scripts" / "apply-branch-protection.sh"
_CI_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


def _load_ruleset() -> dict[str, Any]:
    return json.loads(_RULESET_FILE.read_text(encoding="utf-8"))


def _rule(ruleset: dict[str, Any], rule_type: str) -> dict[str, Any] | None:
    for rule in ruleset["rules"]:
        if rule["type"] == rule_type:
            return rule
    return None


def test_ruleset_targets_the_default_branch() -> None:
    ruleset = _load_ruleset()
    assert ruleset["target"] == "branch"
    assert ruleset["enforcement"] == "active"
    assert "~DEFAULT_BRANCH" in ruleset["conditions"]["ref_name"]["include"]


def test_ruleset_keeps_deletion_and_non_fast_forward_protection() -> None:
    ruleset = _load_ruleset()
    assert _rule(ruleset, "deletion") is not None
    assert _rule(ruleset, "non_fast_forward") is not None


def test_ruleset_requires_pull_requests() -> None:
    ruleset = _load_ruleset()
    assert _rule(ruleset, "pull_request") is not None


def test_ruleset_requires_the_ci_all_checks_gate() -> None:
    ruleset = _load_ruleset()
    rule = _rule(ruleset, "required_status_checks")
    assert rule is not None

    contexts = {check["context"] for check in rule["parameters"]["required_status_checks"]}
    assert "all-checks" in contexts

    ci_workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    assert "all-checks" in ci_workflow["jobs"], (
        "required_status_checks context must match the aggregate CI job name in ci.yml"
    )
    assert "verify" in ci_workflow["jobs"]["all-checks"].get("needs", []), (
        "all-checks gate job must depend on verify job(s)"
    )


def test_ruleset_avoids_broad_bypass_actors() -> None:
    ruleset = _load_ruleset()
    assert ruleset.get("bypass_actors", []) == []


def test_apply_script_exists_and_supports_dry_run() -> None:
    assert _APPLY_SCRIPT.is_file(), "apply-branch-protection.sh must exist"
    script = _APPLY_SCRIPT.read_text(encoding="utf-8")
    assert "main.json" in script
    assert re.search(r"--apply", script), "script must require an explicit --apply flag"
    assert "apply=false" in script


def test_apply_script_dynamic_repo_resolution_and_override() -> None:
    assert _APPLY_SCRIPT.is_file()
    script = _APPLY_SCRIPT.read_text(encoding="utf-8")
    # Must resolve repo dynamically via gh repo view or allow override via --repo
    assert "nameWithOwner" in script
    assert "--repo" in script
    # Must NOT have repo_slug hardcoded unconditionally
    assert 'repo_slug="takurot/agent-scheduler"' not in script


def test_apply_script_supports_ruleset_id_and_detection() -> None:
    assert _APPLY_SCRIPT.is_file()
    script = _APPLY_SCRIPT.read_text(encoding="utf-8")
    # Must support --ruleset-id option
    assert "--ruleset-id" in script
    # Must support PUT when updating existing ruleset
    assert "PUT" in script
    # Must support POST when creating new ruleset
    assert "POST" in script


def test_ci_workflow_has_aggregate_all_checks_job() -> None:
    ci_workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = ci_workflow.get("jobs", {})
    assert "all-checks" in jobs
    assert "verify" in jobs
    aggregate_job = jobs["all-checks"]
    assert "needs" in aggregate_job
    assert "verify" in aggregate_job["needs"]
