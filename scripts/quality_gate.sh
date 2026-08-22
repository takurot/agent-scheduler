#!/usr/bin/env bash
set -euo pipefail

echo "=== Running Ruff linter ==="
uv run ruff check .

echo "=== Running Mypy type checker ==="
uv run mypy src

echo "=== Running Pytest with Coverage Quality Gate (>=80%) ==="
uv run pytest --cov=subsched --cov-report=term-missing --cov-fail-under=80

echo "=== Running Dependency Vulnerability Audit ==="
uv export --frozen --no-hashes --no-emit-project | uvx pip-audit -r /dev/stdin

echo "=== All Quality Gates Passed! ==="
