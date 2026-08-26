from __future__ import annotations

from pathlib import Path

import pytest

from subsched.contract import (
    AgentContractError,
    bootstrap_task_files,
    build_worker_prompt,
    validate_dispatch_preconditions,
)
from subsched.models import Issue, Task


def test_build_worker_prompt_contains_mandatory_instructions() -> None:
    task = Task.from_issue(Issue(number=103, title="Support timeout", body="Some details"))
    prompt = build_worker_prompt(task, verification_commands=("uv run pytest",))

    assert "You are implementing GitHub issue #103." in prompt
    assert "- AGENTS.md" in prompt
    assert "- CLAUDE.md" in prompt
    assert "- .ai/tasks/103.md" in prompt
    assert "- .ai/handoffs/103.md" in prompt
    assert "Work only on issue #103." in prompt
    assert "Do not start another GitHub issue." in prompt
    assert "Do not attempt to merge, create releases, or deploy." in prompt
    assert "uv run pytest" in prompt
    assert "Commit your changes to the current branch" in prompt
    assert "git add" in prompt
    assert "git commit" in prompt
    assert "Do not push or open a pull request." in prompt
    assert "Scheduler's responsibility after verification passes." in prompt
    assert "Do not close the issue" in prompt
    assert "never automatically" in prompt


def test_validate_dispatch_preconditions_fails_if_files_missing(tmp_path: Path) -> None:
    task = Task.from_issue(Issue(number=103, title="Support timeout", body="Some details"))

    with pytest.raises(AgentContractError, match="missing or invalid contract"):
        validate_dispatch_preconditions(tmp_path, task)


def test_validate_dispatch_preconditions_succeeds_after_bootstrap(tmp_path: Path) -> None:
    task = Task.from_issue(Issue(number=103, title="Support timeout", body="Some details"))
    bootstrap_task_files(tmp_path, task)

    # Should not raise
    validate_dispatch_preconditions(tmp_path, task)
