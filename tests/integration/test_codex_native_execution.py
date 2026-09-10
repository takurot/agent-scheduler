from __future__ import annotations

import json
from pathlib import Path

from subsched.agents.codex import parse_codex_jsonl
from subsched.agents.native import NativeWorker
from subsched.contract import bootstrap_task_files
from subsched.models import AgentResultKind, Issue, Task


def test_native_codex_execution_tool_lifecycle_reproduction(tmp_path: Path) -> None:
    """#205 regression test:
    Verify that documented tool events in Codex stream reach PASS, native worker supplies
    --output-schema pointing to a valid schema, and prompt establishes the final result schema
    contract.
    """
    events = [
        {"type": "thread.started", "thread_id": "audit"},
        {"type": "turn.started"},
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": json.dumps({"result": "pass", "summary": "Done"}),
            },
        },
        {"type": "turn.completed", "usage": {}},
    ]

    def parse(es: list[dict[str, object]]) -> AgentResultKind:
        return parse_codex_jsonl("\n".join(map(json.dumps, es)), returncode=0).kind

    tool = {
        "type": "item.started",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "git status --short",
            "status": "in_progress",
        },
    }
    tool_done = {
        "type": "item.completed",
        "item": {
            "id": "item_1",
            "type": "command_execution",
            "command": "git status --short",
            "aggregated_output": "",
            "exit_code": 0,
            "status": "completed",
        },
    }
    normal = [*events[:2], tool, tool_done, *events[2:]]
    prose = [
        *events[:2],
        {
            "type": "item.completed",
            "item": {
                "type": "agent_message",
                "text": "Implemented the change and verified the tests.",
            },
        },
        events[-1],
    ]

    assert parse(events) is AgentResultKind.PASS
    assert parse(normal) is AgentResultKind.PASS
    assert parse(prose) is AgentResultKind.FAILURE

    task = Task.from_issue(Issue(1, "Fixture"), worktree=str(tmp_path))
    bootstrap_task_files(tmp_path, task)

    class Capture:
        def __init__(self) -> None:
            self.req = None

        def execute(self, req):  # type: ignore[no-untyped-def]
            self.req = req
            return parse_codex_jsonl("\n".join(map(json.dumps, normal)), returncode=0)

    capture = Capture()
    worker = NativeWorker(codex_agent=capture, subscription_billing_verified=True)
    native_result = worker.run(task, "codex")

    assert native_result.kind is AgentResultKind.PASS
    assert native_result.output == "codex completed"
    assert capture.req is not None
    assert "--output-schema" in capture.req.argv
    assert '{"result"' in capture.req.stdin_payload.decode("utf-8")
