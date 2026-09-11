#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== Syncing dependencies ==="
uv sync

echo "=== Running Ruff linter ==="
uv run ruff check .

echo "=== Running Mypy type checker ==="
uv run mypy src

echo "=== Running Pytest with Coverage Quality Gate (>=80% line, >=80% branch) ==="
uv run pytest --cov=subsched --cov-report=term-missing --cov-report=json:coverage.json --cov-fail-under=80
uv run python scripts/check_branch_coverage.py coverage.json

echo "=== Running Dependency Vulnerability Audit ==="
uv run pip-audit

echo "=== All Quality Gates Passed! ==="
