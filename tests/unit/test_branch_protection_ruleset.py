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


def test_ruleset_requires_the_ci_verify_check() -> None:
    ruleset = _load_ruleset()
    rule = _rule(ruleset, "required_status_checks")
    assert rule is not None

    contexts = {check["context"] for check in rule["parameters"]["required_status_checks"]}
    assert "verify" in contexts

    ci_workflow = yaml.safe_load(_CI_WORKFLOW.read_text(encoding="utf-8"))
    assert "verify" in ci_workflow["jobs"], (
        "required_status_checks context must match the CI job name in ci.yml"
    )


def test_ruleset_avoids_broad_bypass_actors() -> None:
    ruleset = _load_ruleset()
    assert ruleset.get("bypass_actors", []) == []


def test_apply_script_targets_the_ruleset_file_and_repo() -> None:
    script = _APPLY_SCRIPT.read_text(encoding="utf-8")
    assert "main.json" in script
    assert "takurot/agent-scheduler" in script
    assert re.search(r"--apply", script), "script must require an explicit --apply flag"
    # Default (no --apply) path must not call the mutating gh api methods.
    assert "apply=false" in script
