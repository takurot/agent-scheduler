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

When an agent execution returns a classified provider rate limit (session or weekly):
1. The scheduler captures the `reset_at` timestamp from provider telemetry.
2. The agent is placed into cooldown and remaining tasks fail over to an alternate available agent (e.g. Claude -> Codex).
3. If all agents are exhausted, the scheduler enters `WAITING_CAPACITY` and computes the earliest reset event.
4. Use `subsched run ... --watch` to keep the process alive within its bounded timeout. The
   one-shot default exits after reporting the wait. Watch checks pause state and pending CI at the
   configured poll interval, but does not call the capacity supplier before `wait_duration()`.
5. The current CLI does not have a validated proactive provider-capacity probe. A saved cooldown
   is released only by a fresh `source=provider`, `confidence=high` observation after reset;
   re-running with policy-only availability cannot release it. Until a provider adapter supplies
   that observation, operator review is required rather than automatic resume.

The following Python API is intended for an adapter that supplies a real, validated observation:
```python
scheduler.refresh_capacities([fresh_capacity])
```

`manual_wake()` is a separate, explicit operator override that clears cooldowns without proving
provider availability. It is not exposed by the CLI and must not be used as an automatic reset
path; use it only after independent provider verification and human review.

`--watch-poll-seconds` is limited to 1-300 seconds and `--watch-timeout-seconds` to 1-86400
seconds. `Ctrl-C` exits with code 130 after preserving durable task/worktree state. `subsched pause`
continues to prevent new dispatches while the watch process is alive.

---

## 5. Troubleshooting & Disaster Recovery

### Corrupted State Files
If `state.json` fails schema validation or CRC check:
- The corrupted file is atomically moved to `.ai/quarantine/scheduler-<timestamp>.corrupt.json`.
- A backup from `.ai/backup/scheduler.bak.json` is preserved.
- Inspect the quarantine directory to diagnose schema errors.

### Process Crash & Handoff Recovery
Every dispatch writes `.ai/runtime/<issue>.process.json` (Scheduler PID, start time, agent,
worktree) before the worker runs and removes it once the worker returns. If the scheduler
process crashes unexpectedly while a task is DISPATCHED/IN_PROGRESS:
- On restart, `Scheduler.__init__` reconciles every DISPATCHED/IN_PROGRESS task against its
  recorded process **before** leases are re-registered, so a stale in-flight task can never
  hold a permanent lease and block other READY tasks.
- If the recorded PID is dead (or the record itself is missing — treated as unverifiable and
  handled the same as dead, fail-closed), the task handoff at `.ai/handoffs/<issue>.md` is
  validated and rebuilt, and the task moves to `RETRY`.
- The crash increments `per_agent_failures` for the dispatched Agent. `RETRY` is then resolved
  immediately: below that Agent's `max_agent_failures` budget it returns to `READY`; at or above
  the budget it escalates to `NEEDS_HUMAN`. Task-wide `attempt` and prior verification failures do
  not consume this budget.
- If the persisted `current_agent` / `last_dispatched_agent` identity is missing or inconsistent,
  recovery cannot safely attribute the crash and escalates to `NEEDS_HUMAN` without changing any
  Agent's failure count.
- If the worktree is missing/invalid, the handoff is corrupted, or no worktree was ever
  recorded for the task, it escalates directly to `NEEDS_HUMAN` instead of resuming blindly.
- If the recorded PID is still genuinely alive, the task and its lease are left untouched.

### Repository Instruction Files and Legacy Managed Blocks

`AGENTS.md` and `CLAUDE.md` belong to the target repository. Current `subsched` releases read them
when present but never create, edit, or remove them during dispatch, failover, or recovery.

Task worktrees created by older releases may contain an injected block delimited by
`BEGIN SUBSCHED AGENT CONTRACT` / `END SUBSCHED AGENT CONTRACT`. Do not remove those blocks in bulk:
an operator may have edited the delimited content, and automatic cleanup cannot distinguish those
edits from the historical generated text. For each preserved worktree:

1. inspect `git status --short` and `git diff -- AGENTS.md CLAUDE.md`;
2. compare the block with repository-owned instructions and retained task history;
3. remove it manually only after confirming no user-authored content will be lost;
4. rerun repository verification and keep the worktree/handoff intact.

Rollback must not re-enable automatic injection. A conflict between repository instructions and
the Scheduler execution envelope requires operator review rather than rewriting either file.

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
