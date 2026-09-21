#!/usr/bin/env bash
set -euo pipefail
unset GIT_DIR GIT_WORK_TREE

cd "$(dirname "$0")/.."

echo "=== Syncing dependencies ==="
uv lock --check
uv sync --frozen

echo "=== Running Ruff linter ==="
uv run --frozen ruff check .

echo "=== Running Mypy type checker ==="
uv run --frozen mypy src

echo "=== Running Pytest with Coverage Quality Gate (>=80% line, >=80% branch) ==="
uv run --frozen pytest --cov=subsched --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=80
uv run --frozen python scripts/check_branch_coverage.py coverage.json

echo "=== Running Dependency Vulnerability Audit ==="
uv run --frozen pip-audit

echo "=== All Quality Gates Passed! ==="
