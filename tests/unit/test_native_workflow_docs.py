from pathlib import Path


def test_runbook_scheduler_examples_match_cli_opt_in_contract() -> None:
    text = (Path(__file__).parents[2] / "docs/RUNBOOK.md").read_text(encoding="utf-8")
    section = text.split("### Initializing & Running Scheduler", 1)[1].split("### ", 1)[0]
    commands = [line for line in section.splitlines() if line.startswith("uv run subsched run ")]
    assert commands
    assert any("--dry-run" in line for line in commands)
    assert any("--allow-native" in line for line in commands)
    for line in commands:
        assert "--dry-run" in line or (
            "--allow-native" in line and "--subscription-billing-verified" in line
        )
    assert "isolation" in section.lower()


def test_workflow_does_not_claim_native_execution_is_unimplemented() -> None:
    text = (Path(__file__).parents[2] / "docs/WORKFLOW.md").read_text(encoding="utf-8")
    assert "Phase 1 Queue MVP" not in text
    assert "native実行、branch push、Pull Request作成はまだ有効化しない" not in text
