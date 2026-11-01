from __future__ import annotations

from pathlib import Path

import yaml


def test_audit_workflow_only_uses_dispatch_and_trusted_checkout() -> None:
    path = Path(__file__).resolve().parents[2] / ".github/workflows/independent-audit.yml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    steps = workflow["jobs"]["audit"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["persist-credentials"] == "false"
    audit = next(step for step in steps if step.get("name") == "Run independent audit")
    assert "${{" not in audit["run"]
    assert audit["env"]["SUBSCHED_AUDIT_TARGET"] == "${{ inputs.target }}"
    assert audit["env"]["SUBSCHED_AUDIT_INVENTORY"] == "${{ inputs.inventory }}"
    assert "PYTHONPATH" not in audit["env"]
    assert "-I -S" in audit["run"]
    assert "--no-sync" in audit["run"]
    sync = next(step["run"] for step in steps if step.get("run", "").startswith("uv sync"))
    assert "--no-install-project" in sync and "--no-build" in sync
    assert all(
        "@" not in step.get("uses", "") or len(step["uses"].split("@", 1)[1]) == 40
        for step in steps
    )
