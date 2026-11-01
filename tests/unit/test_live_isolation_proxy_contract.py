from __future__ import annotations

import ast
from pathlib import Path


def test_live_isolation_probe_uses_subscription_endpoint() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "tests/integration/test_native_isolation_container.py"
    ).read_text(encoding="utf-8")
    module = ast.parse(source)
    target = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_real_container_blocks_host_credentials_and_imports_task_commit"
    )
    script = next(
        node.value.value
        for node in target.body
        if isinstance(node, ast.Assign)
        and any(isinstance(name, ast.Name) and name.id == "script" for name in node.targets)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )
    assert "https://chatgpt.com/backend-api/codex" in script
    assert "https://api.openai.com/" not in script
    assert "--noproxy '*'" in script
