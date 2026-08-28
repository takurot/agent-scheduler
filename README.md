# Subscription Agent Scheduler (`subsched`)

`subsched` is a deterministic, subscription-aware coding agent scheduler that turns GitHub Issues into a durable task queue and routes tasks to coding agents (Claude Code, OpenAI Codex) with zero metered API fallback.

---

## Key Features

- 🔒 **Subscription-Only Execution**: Strictly operates within flat-rate subscription quotas (Claude Pro/Team, ChatGPT Plus/Team). Prevents accidental pay-as-you-go API key charges with fail-closed safety.
- 🌳 **Worktree Isolation**: Creates isolated Git worktrees (`subsched/issue-<number>`) for each task to prevent workspace collision.
- ⚡ **Reactive Multi-Agent Failover**: Classifies rate-limit results returned by workers and preserves cooldown/reset state for failover. Proactive provider-capacity monitoring is not yet wired.
- 🛡️ **Automated TDD & Quality Gates**: Enforces test-driven development, running repository verification (`ruff`, `mypy`, `pytest` with $\ge 80\%$ coverage, `pip-audit`) before pull requests.
- 🔀 **Merge Conflict Protection**: Automatically attempts `git rebase main` and cleanly aborts (`git rebase --abort`) on conflict, safely escalating to `NEEDS_HUMAN`.
- 📊 **Observability & Metrics**: Tracks autonomous completion rates, task success metrics, structured JSON Lines event logs, and markdown run reports.

---

## ⚠️ Security Notice: No OS-Level Sandbox

`--allow-native` execution runs the Claude Code / Codex CLI with permission checks bypassed (`bypassPermissions`), so the agent can run Bash commands, edit files, and read files without per-command confirmation. This is required for unattended execution — in non-interactive mode there is no human to confirm anything, so any mode other than `bypassPermissions` denies every action and the agent can do nothing.

**The task's Git worktree is a working-directory default, not an OS-level sandbox.** It does not use a container, chroot, or network isolation. A Bash command run by the agent can technically read or write files outside the worktree and reach the network. Pull request review only inspects the code diff the agent proposes to commit — it does not catch side effects of commands executed during the session (e.g. files touched outside the repo, data sent over the network). Environment variables passed to the agent process are filtered to a small allowlist (no secrets), but this does not restrict filesystem access to files such as `~/.ssh`.

**Only run `--allow-native` against issues and repositories you trust.** Native runs also require `--subscription-billing-verified`; pass it only after independently confirming that each enabled CLI uses subscription billing and that metered/API fallback is disabled. This flag is an operator assertion, not a provider-capacity probe. True OS-level sandboxing (containerized execution, filesystem/network isolation) is a planned hardening item, not yet implemented.

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
uvx agent-scheduler doctor
uvx agent-scheduler run --repo owner/project --issues 101 --dry-run
```

#### Option B: Global CLI install (`uv tool`)
```bash
# Install from source
uv tool install -e .

# Or install from PyPI (once published)
uv tool install agent-scheduler
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
subsched run --repo owner/project --issues 101,102 --allow-native --subscription-billing-verified

# Run by label
subsched run --repo owner/project --label ai-ready --allow-native --subscription-billing-verified

# Run with natural language instruction
subsched run "Execute all open issues" --repo owner/project --allow-native --subscription-billing-verified

# Run using configuration file
subsched run --config subsched.yaml --allow-native --subscription-billing-verified

# Keep the process alive for bounded capacity/CI waiting (default: 1 hour, 30s CI polls)
subsched run --config subsched.yaml --allow-native --subscription-billing-verified --watch
```

Without `--watch`, `run` remains one-shot and reports when durable work is waiting. `--watch`
re-polls pending CI and waits until the next capacity reset, but exits at
`--watch-timeout-seconds`. Capacity is not probed before `Scheduler.wait_duration()` elapses.
Because proactive provider probes are not yet wired, policy-only observations cannot release a
provider cooldown; the bounded watch then exits with state preserved.

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

# Custom test and lint commands for your language. Verification runs each command
# directly (not through a shell), so it must resolve on PATH inside the task worktree.
verification:
  commands:
    - pytest
    - ruff check .
    # For a uv-managed Python project (like this repository), bare `pytest`/`ruff`
    # are only installed inside .venv and won't resolve unless it's activated.
    # Prefix commands with `uv run` instead:
    # - uv run pytest
    # - uv run ruff check .
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
