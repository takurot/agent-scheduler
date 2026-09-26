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
- Docker Engine on Linux or Docker Desktop's Linux engine on macOS for native execution

### Clean Machine Installation
```bash
# Clone repository
git clone https://github.com/takurot/agent-scheduler.git
cd agent-scheduler

# Install dependencies and local package
uv sync

# Run diagnostic verification
uv run subsched doctor
```

---

## 3. Daily Operations

### Health Check (`doctor`)
Runs prerequisite checks, validates the effective native isolation configuration, and
inspects GitHub token scopes. Pass the same custom config used by `run`:
```bash
uv run subsched doctor
uv run subsched doctor --config /absolute/path/to/subsched.yaml
```

Native execution requires the digest-pinned worker and proxy images, a running proxy
attached to both its outbound network and the configured Docker internal network, and
dedicated mode-0700 provider auth directories. The internal network must otherwise be
empty before dispatch. Use the complete schema in `examples/scheduler.yaml`; do not put
SSH, GitHub CLI, or Scheduler write credentials in provider auth directories. A skipped
real-container integration test is not evidence that a deployment is ready.

### Operator-run Docker boundary check

On a trusted host with a Linux Docker engine, use **Actions → Live Docker isolation →
Run workflow**. The job runs only on a self-hosted runner labeled
`subsched-isolation`; do not attach that label to a shared or untrusted runner. Set the
repository variables `SUBSCHED_ISOLATION_WORKER_IMAGE` and
`SUBSCHED_ISOLATION_PROXY_IMAGE` to locally available RepoDigest references. The job
uses synthetic auth and no real provider credentials. Missing settings, a missing image,
any skipped test, or any failed test cause the job to fail.

The job creates its own internal network and proxy, runs all four tests in
`tests/integration/test_native_isolation_container.py`, then removes only the Docker
resources it created. It uploads a 0600 JSON report with commit SHA, Docker version,
image digests, network/proxy configuration, result, and cleanup result. The same check
can be run locally by setting both image variables and a new path in
`SUBSCHED_ISOLATION_REPORT`, then running `bash scripts/live-isolation-check.sh`.

Before a release that claims live Docker isolation validation, the operator must inspect
a successful report for the release commit and intended worker/proxy digests. The normal
CI and release jobs run the opt-in tests as skips; their green status alone does not
establish a live Docker boundary result. A failed or missing report requires another
explicit run after correction.

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

### Explaining Issue Dispatch Readiness
```bash
# Diagnose why an issue is or is not dispatchable
uv run subsched explain 101

# Output machine-readable JSON diagnosis
uv run subsched explain 101 --json
```
Reports whether the issue is eligible, dependency status, provider routing with freshness, cooldowns, run budget, and recommended operator actions without modifying scheduler state or making paid provider probes.

### Generating Metrics & Reports
```bash
# Console summary
uv run subsched metrics

# Structured JSON metrics
uv run subsched metrics --json

# Save markdown run report
uv run subsched metrics --report run_report.md
```

### Local Web Dashboard

```bash
# Start the dashboard and open it in a browser (default: http://127.0.0.1:8080)
uv run subsched dashboard

# Start without opening a browser, on a specific port, with a slower poll interval
uv run subsched dashboard --no-browser --port 9000 --interval 5
```

The dashboard is read-only (no state-changing endpoints exist) and is implemented
entirely with the Python standard library (`http.server.ThreadingHTTPServer`) -- no
extra dependencies are installed. It binds to `127.0.0.1` by default and requires the
ephemeral `?token=...` printed to the console on startup; requests with a missing or
incorrect token, or a forged `Host` header, are rejected. If the requested `--port` is
already in use, the next free port is used automatically. It never acquires the
scheduler's lock, so it is always safe to run alongside `subsched run`.

If `.ai/` has not been initialized yet, or the scheduler state was quarantined after
corruption (`StateCorruptionError`), the dashboard shows an in-browser banner instead
of failing -- run `subsched init` or follow the recovery steps in
[Troubleshooting & Disaster Recovery](#5-troubleshooting--disaster-recovery) as
appropriate.

`--host` only accepts `127.0.0.1` and `localhost` as *client-facing* hostnames: the
server's own `Host` header validation rejects every request whose `Host` header isn't
one of those two (with or without the port), by design (DNS rebinding protection).
Passing a non-loopback `--host` (e.g. `0.0.0.0`) still binds the socket there, but
every request will then be rejected with 400, so it is not a supported way to expose
the dashboard beyond localhost.

### Pause, Resume & Task Cancellation
```bash
# Emergency pause
uv run subsched pause

# Resume execution
uv run subsched resume

# Cancel specific issue while preserving worktree state
uv run subsched cancel 101
```

### Reconciling `READY_FOR_REVIEW` Tasks Against Merged PRs

Under the default `execution.ci_monitoring: false`, the scheduler never revisits a task
once its PR is created, so tasks accumulate under `READY_FOR_REVIEW` even after a
maintainer merges (or closes) the PR on GitHub. Run `subsched reconcile` periodically
(e.g. from cron or CI) to close that gap:

With CI monitoring enabled, a PASS is recorded as `ci_result: PASS` while the task
remains `READY_FOR_REVIEW`. It does not release dependent Issues. Existing PR-backed
`COMPLETE` records without merge evidence are read as `READY_FOR_REVIEW`; run
`subsched reconcile` to confirm their current GitHub state. Local-only tasks without a
PR remain `COMPLETE` after local verification.

```bash
# Preview planned transitions without mutating .ai/scheduler.json
uv run subsched reconcile --dry-run

# Apply reconciliation: merged PR -> COMPLETE, unmerged closed PR -> NEEDS_HUMAN,
# open PR -> left unchanged
uv run subsched reconcile

# Also remove the worktree of any task that reconciles to COMPLETE, but only when
# `git status --porcelain` reports no changes at all
uv run subsched reconcile --prune-worktrees
```

`subsched reconcile` fails closed: any `gh` execution error, authentication failure, or
malformed output leaves `.ai/scheduler.json` untouched and exits non-zero. It is never
invoked implicitly by `subsched run` or the discovery loop.

### Diagnosing Disk Usage and Archiving Completed Worktrees

Start with a read-only report. `--json` is suitable for operator tooling and contains
the same candidates and retention reasons as the text output:

```bash
uv run subsched maintenance --dry-run
uv run subsched maintenance --dry-run --json
```

The report inventories Scheduler state/artifacts, runtime logs, quarantine, worktrees,
and prior archives. `CANCELLED`, `NEEDS_HUMAN`, active, unmerged, dirty, untracked,
symlinked, identity-conflicted, and orphaned worktrees are retained. An unreadable or
unknown Git result is also a retention reason. Do not delete history to work around a
run dispatch budget; `execution.max_tasks_per_run` counts distinct issues first
dispatched by the current run, not persisted history.

After reviewing every candidate, apply the current plan explicitly:

```bash
uv run subsched maintenance --apply
```

Apply reloads state and repeats all checks while holding the Scheduler lock. For each
still-eligible task it creates `.ai/archive/issue-<number>/` containing
`repository.bundle`, `manifest.json`, `RESTORE.md`, and available Scheduler recovery
artifacts, then invokes `git worktree remove` without force. It does not delete the Task
record, dependency information, branch, global handoff/task history, logs, or quarantine.
If removal fails, the archive and worktree are both retained; inspect them manually and
do not remove either until the cause is understood.

To restore, first read the archive's `manifest.json` and `RESTORE.md`. When the recorded
task branch still exists and no destination is present, the documented flow is:

```bash
git worktree add .ai/worktrees/issue-101 subsched/issue-101
cp -R .ai/archive/issue-101/artifacts/. .ai/worktrees/issue-101/.ai/
```

If the branch is missing, inspect `repository.bundle` and fetch it to a new,
operator-chosen branch; never overwrite an existing branch or worktree. Confirm the
restored worktree path, branch, status, task/handoff files, and Scheduler state before
resuming any operation.

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

### Resolving NEEDS_HUMAN Tasks and Restoring State
After manually diagnosing and remediating the root cause in the worktree:
```bash
# Preview resolution
uv run subsched resolve 101 --note "Fixed lockfile conflict" --dry-run

# Apply resolution and record sanitized audit log under .ai/audit/resolutions.jsonl
uv run subsched resolve 101 --note "Fixed lockfile conflict"
```
To safely recover scheduler state from a validated backup or quarantine snapshot:
```bash
# Validate and restore snapshot
uv run subsched restore-state .ai/backups/scheduler.bak.json --dry-run
uv run subsched restore-state .ai/backups/scheduler.bak.json
```


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

---

## 7. Model Context Protocol (MCP) Server

`subsched` provides a Model Context Protocol (MCP) server running over standard I/O (`stdio`), allowing AI assistants (such as Claude Desktop, Cursor, and Antigravity) to monitor queue state, bootstrap repositories, and trigger dispatches across local codebases.

### Requirements & Installation
The MCP server requires the optional `mcp` extra:
```bash
pip install agent-scheduler[mcp]
# or with uv:
uv add --extra mcp agent-scheduler
```

### Launching the Server
```bash
# Launch with the current directory as the default repository
subsched mcp

# Launch with an explicit repository path
subsched --repository /path/to/repo mcp
```

### Client Configuration

#### 1. Claude

##### Claude Desktop
Add the server entry to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):
```json
{
  "mcpServers": {
    "subsched": {
      "command": "uvx",
      "args": ["--from", "agent-scheduler[mcp]", "subsched", "mcp"]
    }
  }
}
```

##### Claude Code (CLI)
Register using the CLI command:
```bash
# Project-level (.mcp.json in current repository):
claude mcp add subsched --scope project -- uvx --from "agent-scheduler[mcp]" subsched mcp

# User-level (~/.claude.json across all repositories):
claude mcp add subsched --scope user -- uvx --from "agent-scheduler[mcp]" subsched mcp
```
Or commit a `.mcp.json` at your repository root:
```json
{
  "mcpServers": {
    "subsched": {
      "command": "uvx",
      "args": ["--from", "agent-scheduler[mcp]", "subsched", "mcp"]
    }
  }
}
```

#### 2. OpenAI Codex

##### Codex CLI
Register using the CLI command:
```bash
codex mcp add subsched -- uvx --from "agent-scheduler[mcp]" subsched mcp
```
Or configure `~/.codex/config.toml` (global) or `<repo>/.codex/config.toml` (project-specific):
```toml
[mcp_servers.subsched]
command = "uvx"
args = ["--from", "agent-scheduler[mcp]", "subsched", "mcp"]
```

#### 3. Gemini / Antigravity (`agy`)

##### Antigravity CLI (`agy`) / IDE
Add the server entry to `~/.gemini/config/mcp_config.json` (global) or `.agents/mcp_config.json` (project-specific):
```json
{
  "mcpServers": {
    "subsched": {
      "command": "uvx",
      "args": ["--from", "agent-scheduler[mcp]", "subsched", "mcp"]
    }
  }
}
```

#### 4. Cursor
Add the server configuration to `.cursor/mcp.json`:
```json
{
  "mcpServers": {
    "subsched": {
      "command": "subsched",
      "args": ["mcp"]
    }
  }
}
```

> **Tip:** If `agent-scheduler[mcp]` is installed locally or via `pipx` / `uv tool install`, you can use `"command": "subsched", "args": ["mcp"]`. Pass `"--repository", "/path/to/repo"` before `"mcp"` to pin the server to a specific repository.

### Tool Discovery & Server Instructions

The server initializes `FastMCP("subsched", instructions=...)` with a summary of the standard
orchestration workflow (`init_repo` → `queue_issues` → `trigger_dispatch` → `get_status` /
`inspect_task` → `resolve_needs_human`), and every tool below registers a non-empty
`description` plus per-parameter `Field(description=...)` documentation in its `tools/list`
schema. Clients that surface server instructions and tool/parameter descriptions (Claude
Desktop, Cursor, Antigravity/Gemini) can therefore infer the workflow, argument formats (e.g.
`issues="123,124"` or `"all-open"`), and safety gates (`allow_native`,
`subscription_billing_verified`) without prior knowledge of the `subsched` CLI or source code.

### Exposed Capabilities

#### Tools
- **`subsched_get_status`**: Retrieve current queue breakdown, active cooldowns, and task lists. Supports optional `repository_path` and `verbose` flag.
- **`subsched_inspect_task`**: Fetch complete task detail, parsed semantic handoff (`.ai/handoffs/<issue>.md`), and recent worktree commits.
- **`subsched_queue_issues`**: Discover issues from GitHub and queue them atomically. Supports `dry_run=True` to preview discoveries without state mutations.
- **`subsched_trigger_dispatch`**: Non-blocking background dispatch of `subsched run`. Returns a persistent `run_id` with `accepted` status; this only means the launch request was accepted. Requires explicit `allow_native=True` and `subscription_billing_verified=True` for live worker execution (fails closed by default).
- **`subsched_get_dispatch_run`**: Read `accepted`, `starting`, `running`, `succeeded`, `failed`, or `stale` status for that `run_id` without waiting for the worker. `subsched dispatch-status RUN_ID --json` provides the same CLI view. PID start time is checked to detect PID reuse. A fixed failure reason and exit code are returned; raw provider output is never exposed. Private lifecycle logs live under `.ai/runtime/dispatch-runs/` with 0600 permissions and bounded rotation.
- **`subsched_init_repo`**: Scaffold `subsched.yaml`, `AGENTS.md`, and `CLAUDE.md` in the target repository.
- **`subsched_resolve_needs_human`**: Transition a remediated task from `NEEDS_HUMAN` back to `READY`.
- **`subsched_cancel_task`**: Transition task to `CANCELLED` while strictly preserving worktree and handoff files.
- **`subsched_control`**: Cleanly pause or resume task dispatching (`action: "pause" | "resume"`).
- **`subsched_get_metrics`**: Calculate Productivity, Reliability, and Capacity metrics.
- **`subsched_reconcile`**: Reconcile `READY_FOR_REVIEW` tasks against actual PR state on GitHub -- merged PRs advance to `COMPLETE`, unmerged closed PRs escalate to `NEEDS_HUMAN`, open PRs are left unchanged. Fails closed on any `gh` error. Supports `dry_run=True`. Worktree pruning is CLI-only (`subsched reconcile --prune-worktrees`), never exposed via MCP.

#### Resources
- `subsched://queue`: JSON snapshot of all tasks and scheduler pause state.
- `subsched://capacities`: JSON snapshot of active agent cooldowns and provider reset times.
- `subsched://tasks/{issue}/handoff`: Markdown content of the task's semantic handoff document.
- `subsched://guidelines`: Standard guidelines and invariants for coding agents.

#### Prompts
- `triage_task`: Prompt guiding the LLM to inspect handoffs and git commits for `NEEDS_HUMAN` issues before remediating.
- `bootstrap_repo`: Prompt guiding the LLM to inspect stack tooling and initialize `subsched` configuration.
