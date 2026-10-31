#!/usr/bin/env bash
set -euo pipefail
unset GIT_DIR GIT_WORK_TREE

cd "$(dirname "$0")/.."

echo "=== Syncing dependencies ==="
if [[ "${SUBSCHED_OFFLINE_PYTHON:-}" == "1" ]]; then
    offline_root="$(mktemp -d /tmp/subsched-offline.XXXXXXXX)"
    export UV_PROJECT_ENVIRONMENT="$offline_root/venv"
    export UV_CACHE_DIR="$offline_root/cache"
    export UV_NO_SYNC=1 UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never
fi
uv lock --check
if [[ "${SUBSCHED_OFFLINE_PYTHON:-}" == "1" ]]; then
    python3 scripts/offline_verification.py /opt/subsched-offline "$UV_PROJECT_ENVIRONMENT" "$UV_CACHE_DIR"
else
    uv sync --frozen
fi

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
