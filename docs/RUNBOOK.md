# Operator Runbook

This runbook guides operators through running, monitoring, troubleshooting, and recovering the **Subscription Agent Scheduler (`subsched`)**.

---

## 1. System Overview

`subsched` is a deterministic, subscription-aware agent scheduler designed to maximize throughput of subscription coding agents (Claude Code, OpenAI Codex) while preventing metered API fallback, rate limit exhaustion, and unsafe concurrency races.

### Key Invariants
- **Subscription-only execution:** Never incurs metered API billing or unauthorized fallbacks.
- **Fail-closed operations:** Any corrupted state, invalid token, or unknown exit status halts execution safely.
- **Durable queue:** State persisted atomically in `.ai/scheduler.json` with revision tracking and recovery locks.
- **Atomic task leasing:** Each active worktree and agent is leased exclusively to prevent duplicate execution.

---

## 2. Setup & Installation

### Requirements
- Python `>=3.12`
- [`uv`](https://docs.astral.sh/uv/)
- Git `>=2.40`
- GitHub CLI (`gh`)
- Coding agent CLI tools: `claude`, `codex` (optional: `ccusage`)

### Clean Machine Installation
```bash
# Clone repository
git clone https://github.com/takurot/agent-scheduler.git
cd agent-scheduler

# Install dependencies and local package
uv sync --extra dev

# Run diagnostic verification
uv run subsched doctor
```

---

## 3. Daily Operations

### Health Check (`doctor`)
Runs prerequisite checks on local executables and inspects GitHub token scopes:
```bash
uv run subsched doctor
```

### Initializing & Running Scheduler
```bash
# Natural language instruction discovery
uv run subsched run "Run all open issues"

# Discover specific issues in dry-run mode
uv run subsched run --repo owner/repo --issues 101,102,103 --dry-run

# Run with custom config
uv run subsched run --config subsched.yaml
```

### Checking Queue & Worker Status
```bash
uv run subsched status
```
Outputs:
- Current scheduler state (running / paused / waiting for capacity)
- Active cooldowns and earliest reset timestamps
- Queue breakdown by state (`READY`, `IN_PROGRESS`, `VERIFYING`, `NEEDS_HUMAN`, `COMPLETE`)

### Generating Metrics & Reports
```bash
# Console summary
uv run subsched metrics

# Structured JSON metrics
uv run subsched metrics --json

# Save markdown run report
uv run subsched metrics --report run_report.md
```

### Pause, Resume & Task Cancellation
```bash
# Emergency pause
uv run subsched pause

# Resume execution
uv run subsched resume

# Cancel specific issue while preserving worktree state
uv run subsched cancel 101
```

---

## 4. Capacity Failover & Wait Scheduling

When an agent encounters provider rate limits (session or weekly):
1. The scheduler captures the `reset_at` timestamp from provider telemetry.
2. The agent is placed into cooldown and remaining tasks fail over to an alternate available agent (e.g. Claude -> Codex).
3. If all agents are exhausted, the scheduler enters `WAITING_CAPACITY` and computes the earliest reset event.
4. When the reset time arrives, a fresh capacity probe is evaluated before resuming execution.

To manually wake or refresh capacity:
```python
scheduler.refresh_capacities([fresh_capacity])
scheduler.manual_wake()
```

---

## 5. Troubleshooting & Disaster Recovery

### Corrupted State Files
If `state.json` fails schema validation or CRC check:
- The corrupted file is atomically moved to `.ai/quarantine/scheduler-<timestamp>.corrupt.json`.
- A backup from `.ai/backup/scheduler.bak.json` is preserved.
- Inspect the quarantine directory to diagnose schema errors.

### Process Crash & Handoff Recovery
If the scheduler host or agent subprocess crashes unexpectedly:
- On restart, `reconcile_task_recovery` checks the process PID via `.ai/runtime/<issue>.process.json`.
- If dead, the task handoff at `.ai/handoffs/<issue>.md` is validated and rebuilt.
- The task transitions to `RETRY` and execution resumes safely in the assigned worktree.

### Merge Conflicts (`NEEDS_HUMAN`)
When a task branch conflicts with the base branch:
- `rebase_onto_base` detects the conflict and immediately executes `git rebase --abort`.
- The worktree is preserved in a clean state.
- The task is escalated to `NEEDS_HUMAN` for manual review.

---

## 6. Release & Distribution

### PyPI Trusted Publishing
Releases are automatically built and published to PyPI via GitHub Actions when a version tag is pushed:

```bash
# 1. Update version in pyproject.toml
# 2. Commit and tag release
git tag v0.1.0

# 3. Push tag to GitHub
git push origin v0.1.0
```

### Installation from PyPI
Once published, users can run `subsched` without manual cloning:

```bash
# Run immediately without installation (like npx)
uvx agent-scheduler run --help

# Global installation
uv tool install agent-scheduler
subsched doctor
```
