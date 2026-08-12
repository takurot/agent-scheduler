# Subscription Agent Scheduler

`subsched` is a deterministic local scheduler that turns GitHub issues into a durable task
queue and routes one issue at a time to an available coding-agent worker.

This repository currently implements the safe Phase 1 Queue MVP from
[`docs/SPEC.md`](docs/SPEC.md). It uses scripted workers by default: it does **not** invoke
Claude Code or Codex, push branches, create pull requests, or incur metered API usage.

## Requirements

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- Git and GitHub CLI for later native-adapter phases

## Setup

```bash
uv sync --extra dev
uv run subsched doctor
uv run pytest
```

Use `uv run subsched --help` for the CLI contract. A mock queue can be initialized without
calling GitHub:

```bash
uv run subsched run --repo owner/project --issues 101,103 --dry-run
uv run subsched status
```

Runtime state is written atomically beneath `.ai/`. Do not delete it while the scheduler is
running. Native Claude/Codex execution and GitHub writes remain fail-closed until later phases.
