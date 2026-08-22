# Subscription Agent Scheduler (`subsched`)

`subsched` is a deterministic, subscription-aware coding agent scheduler that turns GitHub Issues into a durable task queue and routes tasks to coding agents (Claude Code, OpenAI Codex) with zero metered API fallback.

---

## Key Features

- 🔒 **Subscription-Only Execution**: Strictly operates within flat-rate subscription quotas (Claude Pro/Team, ChatGPT Plus/Team). Prevents accidental pay-as-you-go API key charges with fail-closed safety.
- 🌳 **Worktree Isolation**: Creates isolated Git worktrees (`subsched/issue-<number>`) for each task to prevent workspace collision.
- ⚡ **Multi-Agent Capacity & Failover**: Monitors rate limits across providers in real time, automatically failing over between agents (Claude ↔ Codex) or pausing until reset time.
- 🛡️ **Automated TDD & Quality Gates**: Enforces test-driven development, running repository verification (`ruff`, `mypy`, `pytest` with $\ge 80\%$ coverage, `pip-audit`) before pull requests.
- 🔀 **Merge Conflict Protection**: Automatically attempts `git rebase main` and cleanly aborts (`git rebase --abort`) on conflict, safely escalating to `NEEDS_HUMAN`.
- 📊 **Observability & Metrics**: Tracks autonomous completion rates, task success metrics, structured JSON Lines event logs, and markdown run reports.

---

## Requirements

- Python `>=3.12`
- [`uv`](https://docs.astral.sh/uv/)
- Git `>=2.40`
- GitHub CLI (`gh`)
- Coding agent CLI tools: `claude` and/or `codex`

---

## Quickstart

### 1. Installation

#### Option A: Run immediately without installation (`uvx`)
```bash
uvx subscription-agent-scheduler doctor
uvx subscription-agent-scheduler run --repo owner/project --issues 101 --dry-run
```

#### Option B: Global CLI install (`uv tool`)
```bash
# Install from source
uv tool install -e .

# Or install from PyPI (once published)
uv tool install subscription-agent-scheduler
```

#### Option C: Development setup
```bash
git clone https://github.com/takurot/agent-scheduler.git
cd agent-scheduler
uv sync --extra dev
uv run subsched doctor
```

---

## Usage

### Environment Diagnostic
Verify local executables and GitHub token scope:
```bash
subsched doctor
```

### Running Tasks

#### Dry-run Mode (Safe Queue Preview)
Discovers issues and populates the durable queue without calling agent subprocesses or creating PRs:
```bash
subsched run --repo owner/project --issues 101,102 --dry-run
```

#### Live Agent Execution (`--allow-native`)
Executes coding agents in subscription mode, runs tests, and creates Pull Requests:
```bash
# Run specific issues
subsched run --repo owner/project --issues 101,102 --allow-native

# Run by label
subsched run --repo owner/project --label ai-ready --allow-native

# Run with natural language instruction
subsched run "Execute all open issues" --repo owner/project --allow-native

# Run using configuration file
subsched run --config subsched.yaml --allow-native
```

### Monitoring & Operations

```bash
# Check queue and cooldown status
subsched status

# View Productivity, Reliability, and Capacity metrics
subsched metrics
subsched metrics --json
subsched metrics --report run_report.md

# Emergency pause and resume
subsched pause
subsched resume

# Cancel a specific task while preserving worktree state
subsched cancel 101
```

---

## Configuration (`subsched.yaml`)

You can place a `subsched.yaml` in your project root to customize repositories, concurrency, and verification commands:

```yaml
github:
  repo: owner/project
  mode: all-open # or label: "ai-ready"

execution:
  concurrency: 1

# Custom test and lint commands for your language
verification:
  commands:
    - pytest
    - ruff check .
    # For Node.js / TypeScript:
    # - npm test
    # - npm run lint
```

---

## CLI Reference

| Command | Description |
|---|---|
| `subsched doctor` | Check prerequisite binaries and inspect GitHub token scope |
| `subsched run` | Discover issues, initialize queue, and dispatch tasks |
| `subsched status` | Display queue breakdown, cooldowns, and scheduler state |
| `subsched metrics` | Output Productivity, Reliability, and Capacity metrics |
| `subsched pause` | Pause task execution cleanly after current step |
| `subsched resume` | Resume scheduler execution from paused state |
| `subsched cancel <id>` | Cancel a task and preserve its worktree files |

---

## Documentation

- [`docs/SPEC.md`](docs/SPEC.md): Ground-truth specification and safety invariants.
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md): Operator runbook for running, monitoring, and disaster recovery.
- [`docs/WORKFLOW.md`](docs/WORKFLOW.md): Contributor and development workflow.
