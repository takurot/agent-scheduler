# Subscription Agent Scheduler (`subsched`)

`subsched` is a deterministic, subscription-aware coding agent scheduler that turns GitHub Issues into a durable task queue and routes tasks to coding agents (Claude Code, OpenAI Codex) with zero metered API fallback.

> [!NOTE]
> **Package & Command Mapping**:
> - **PyPI package**: `agent-scheduler` (`pip install agent-scheduler`)
> - **Python import**: `subsched` (`import subsched`)
> - **CLI command**: `subsched` (primary) or `agent-scheduler` (alias)

---

## Key Features

- 🔒 **Subscription-Only Execution**: Strictly operates within flat-rate subscription quotas (Claude Pro/Team, ChatGPT Plus/Team). Prevents accidental pay-as-you-go API key charges with fail-closed safety.
- 🌳 **Worktree Isolation**: Creates isolated Git worktrees (`subsched/issue-<number>`) for each task to prevent workspace collision.
- ⚡ **Reactive Multi-Agent Failover**: Classifies rate-limit results returned by workers and preserves cooldown/reset state for failover. Proactive provider-capacity monitoring is not yet wired.
- 🛡️ **Automated TDD & Quality Gates**: Enforces test-driven development, running repository verification (`ruff`, `mypy`, `pytest` with $\ge 80\%$ coverage, `pip-audit`) before pull requests.
- 🔀 **Merge Conflict Protection**: Resolves the repository default branch, fetches its latest `origin` state, rebases onto that remote-tracking ref, and cleanly aborts on conflict before escalating to `NEEDS_HUMAN`.
- 📊 **Observability & Metrics**: Tracks autonomous completion rates, task success metrics, structured JSON Lines event logs, and markdown run reports.

---

## ⚠️ Security Notice: No OS-Level Sandbox

`--allow-native` execution runs the Claude Code / Codex CLI with permission checks bypassed (`bypassPermissions`), so the agent can run Bash commands, edit files, and read files without per-command confirmation. This is required for unattended execution — in non-interactive mode there is no human to confirm anything, so any mode other than `bypassPermissions` denies every action and the agent can do nothing.

**The task's Git worktree is a working-directory default, not an OS-level sandbox.** It does not use a container, chroot, or network isolation. A Bash command run by the agent can technically read or write files outside the worktree and reach the network. Pull request review only inspects the code diff the agent proposes to commit — it does not catch side effects of commands executed during the session (e.g. files touched outside the repo, data sent over the network). Environment variables passed to the agent process are filtered to a small allowlist (no secrets), but this does not restrict filesystem access to files such as `~/.ssh`.

**Only run `--allow-native` against issues and repositories you trust.** Native runs also require `--subscription-billing-verified`; pass it only after independently confirming that each enabled CLI uses subscription billing and that metered/API fallback is disabled. This flag is an operator assertion, not a provider-capacity probe. True OS-level sandboxing (containerized execution, filesystem/network isolation) is a planned hardening item, not yet implemented.

Repository instruction files are user-owned input. `subsched` tells each worker to read an existing
`AGENTS.md` and `CLAUDE.md`, but never creates, edits, or removes either file. Task scope and
Scheduler/worker responsibilities are delivered through the generated worker prompt and `.ai/`
task/handoff state. If repository instructions conflict with the Scheduler's issue, billing,
permission, or lifecycle boundaries, the worker must stop and report the conflict; existing runtime
gates remain authoritative.

---

## Requirements

> [!IMPORTANT]
> **Python `>=3.12` is strictly required.** Earlier Python versions are unsupported.

- **Python**: `>=3.12`
- **Supported OS**: Linux and macOS (Windows is unsupported due to POSIX process group session, signal handling, and filesystem isolation requirements)
- **Package Manager**: `pip`, `pipx`, or [`uv`](https://docs.astral.sh/uv/)
- **Git**: `>=2.40`
- **GitHub CLI**: `gh` (authenticated)
- **Coding agent CLI tools**: `claude` (Claude Code) and/or `codex` (OpenAI Codex)

---

## Quickstart

### 1. Installation

#### Option A: Install via `pip` or `pipx` (Standard)
```bash
# Recommended for standalone CLI installation:
pipx install agent-scheduler

# Or install into your active Python environment via pip:
pip install agent-scheduler
```

> [!NOTE]
> Installing the `agent-scheduler` package provides both the `subsched` command (primary) and the `agent-scheduler` alias.

#### Option B: Run immediately without installation (`uvx`)
```bash
uvx agent-scheduler doctor
uvx agent-scheduler run --repo owner/project --issues 101 --dry-run
```

#### Option C: Global CLI install via `uv tool`
```bash
# Install from PyPI
uv tool install agent-scheduler

# Or install editable from local source
uv tool install -e .
```

#### Option D: Development setup (from source)
```bash
git clone https://github.com/takurot/agent-scheduler.git
cd agent-scheduler
uv sync --extra dev
uv run subsched doctor
```

---

## Usage

### Bootstrapping a New Repository
Scaffold `subsched.yaml`, `AGENTS.md`, and `CLAUDE.md`, auto-detecting the project's
stack (Python/Node/Go/Rust) and GitHub repo slug:
```bash
subsched init

# Preview without writing to disk
subsched init --dry-run

# Overwrite existing files
subsched init --force
```

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

### Model Context Protocol (MCP) Server

Run `subsched` as an MCP server over stdio to integrate directly with AI assistants and IDEs (Claude Desktop, Cursor, Antigravity):

```bash
# Run over stdio (requires mcp extra: pip install agent-scheduler[mcp])
subsched mcp

# Target a specific repository
subsched --repository /path/to/repo mcp
```

#### Client Configuration Examples

**Claude Desktop** (`claude_desktop_config.json`):
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

**Cursor** (`.cursor/mcp.json`):
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

#### Exposed Tools, Resources, and Prompts
- **Tools**: `subsched_get_status`, `subsched_inspect_task`, `subsched_queue_issues`, `subsched_trigger_dispatch` (non-blocking background dispatch), `subsched_init_repo`, `subsched_resolve_needs_human`, `subsched_cancel_task`, `subsched_control`, `subsched_get_metrics`. All tools accept an optional `repository_path`.
- **Resources**: `subsched://queue`, `subsched://capacities`, `subsched://tasks/{issue}/handoff`, `subsched://guidelines`.
- **Prompts**: `triage_task` (diagnose and remediate `NEEDS_HUMAN` issues), `bootstrap_repo` (scaffold repository configuration).

---

## Configuration (`subsched.yaml`)

You can place a `subsched.yaml` in your project root to customize repositories, concurrency, and verification commands:

```yaml
github:
  repo: owner/project
  # Optional. Omit to resolve the repository default branch via GitHub.
  # base_branch: develop
  # Optional label filtering:
  # include_labels: ["ai-ready"]  # AND semantics: must match all
  # exclude_labels: ["blocked"]   # OR semantics: excluded if any match
  completion:
    create_pr: true
    # When true, appends "Closes #<issue>" to the PR body to automatically close the issue upon merge
    close_issue: false

# Supported agents: claude, codex. At least one agent must remain enabled.
agents:
  claude:
    enabled: true
    priority: 100
  codex:
    enabled: false
    priority: 90

routing:
  strategy: capacity-aware
  provider_capacity:
    preferred: true
  local_estimate:
    proactive_switch: false

billing:
  api_fallback: false
  metered_usage: false
  unknown_mode: disable

execution:
  concurrency: 1
  max_agent_switches: 6
  max_tasks_per_run: 50
  pause_running_policy: continue

queue:
  priority:
    label_scores: {}
    tie_break: issue_number_asc

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

`verification.commands` must contain at least one non-blank command. An empty list or blank-only
entry is rejected instead of being treated as a successful verification run.

`github.base_branch` is optional. When PR creation is enabled and it is omitted, `subsched`
resolves `defaultBranchRef` with `gh repo view`. Resolution failure or an unsafe branch name
fails closed; it never falls back implicitly to `main`. Before rebase, `subsched` fetches the
resolved branch from `origin` and rebases onto the remote-tracking ref.

The values shown for `billing.*`, `routing.*`, `pause_running_policy`, `tie_break`, and
`close_issue` are the only currently supported values. Unsupported alternatives fail during
configuration loading instead of being accepted and ignored.

---

## Guiding Coding Agents (`AGENTS.md` & `CLAUDE.md`)

When `subsched` dispatches a task to a coding agent (`claude` or `codex`), it constructs a strict prompt enforcing issue scope, worktree boundaries, and security invariants. Crucially, the prompt directs each worker to read repository instructions:

```text
Read repository instructions when present:
- AGENTS.md
- CLAUDE.md
Read the project documentation required by those instructions.
```

`subsched` treats `AGENTS.md` (for OpenAI Codex, Cursor, and emerging agents) and `CLAUDE.md` (for Claude Code) as **user-owned input**; it never edits or removes either file. Providing these files in your repository root allows you to control how agents implement features, run tests, and format commits.

### Recommended Content for Instruction Files

1. **Development Workflow & Source of Truth**:
   - Point agents to your project's workflow and architecture documents (e.g., [`docs/WORKFLOW.md`](docs/WORKFLOW.md) and [`docs/SPEC.md`](docs/SPEC.md)).
   - Specify repository conventions, coding standards, and directory layouts.

2. **Testing & Quality Gates**:
   - Explicitly instruct agents to write tests first (TDD: Red → Green → Refactor) and run verification before completing their work.
   - Align the agent's verification instructions with the `verification.commands` in your `subsched.yaml` (e.g. `uv run pytest`, `uv run ruff check .`, `npm test`).
   - Remind agents that `subsched` re-runs these commands in the post-worker gate, and the task will fail if tests or linters fail.

3. **Behavioral Principles**:
   - **Simplicity First**: Instruct agents to write the minimum code that solves the issue without speculative features or premature abstractions.
   - **Surgical Changes**: Restrict changes only to what is directly required for the assigned issue; prohibit unrelated refactoring or cleaning up existing dead code.

4. **Commit Message Rules (Crucial Safety Rule)**:
   - Request Conventional Commits (e.g., `feat:`, `fix:`, `refactor:`, `test:`, `docs:`).
   - **NEVER use GitHub auto-close keywords**: Explicitly forbid keywords such as `Fixes #<number>`, `Closes #<number>`, or `Resolves #<number>` (any casing or inflection) in commit messages. `subsched` enforces manual review before issues are closed; commit messages containing auto-close keywords trigger an invariant violation that blocks Git push and Pull Request creation. Tell agents to use plain references like `issue #<number>` or `Implements work for #<number>`.

5. **Semantic Handoff Integrity**:
   - If a multi-step task is interrupted or switches agents due to quota limits, workers communicate state through `.ai/handoffs/<issue>.md`.
   - Instruct agents to preserve all 8 required section headers (`## Goal`, `## Current Plan`, `## Completed`, `## Current Work`, `## Decisions`, `## Known Broken State`, `## Next Action`, `## Timestamp`) and use strict ISO 8601 timestamps.

### Example Implementations

See this repository's own [`AGENTS.md`](AGENTS.md) and [`CLAUDE.md`](CLAUDE.md) for complete, production-tested examples of repository instruction files.

---

## CLI Reference

| Command | Description |
|---|---|
| `subsched init` | Scaffold `subsched.yaml`, `AGENTS.md`, and `CLAUDE.md` for a new repository (`--repo`, `--agents-md/--no-agents-md`, `--claude-md/--no-claude-md`, `--force`, `--dry-run`) |
| `subsched doctor` | Check prerequisite binaries (`git`, `gh`, `claude`, `codex`) and inspect GitHub token scope |
| `subsched run` | Discover issues, initialize queue, and dispatch tasks (`--allow-native`, `--subscription-billing-verified`, `--watch`, `--dry-run`) |
| `subsched status` | Display queue breakdown, cooldowns, and scheduler state (`-v` / `--verbose` for per-task detail) |
| `subsched metrics` | Output Productivity, Reliability, and Capacity metrics (`--json`, `--report <file.md>`) |
| `subsched pause` | Pause task execution cleanly after current step |
| `subsched resume` | Resume scheduler execution from paused state |
| `subsched cancel <id>` | Cancel a task and preserve its worktree files |
| `subsched mcp` | Run subsched as an MCP server over stdio (`agent-scheduler[mcp]`) |

---

## Frequently Asked Questions (FAQ)

### 1. Does `subsched` switch agents (e.g. Claude to Codex) when token limits or subscription quotas are exhausted?
Yes. `subsched` supports multi-agent configurations (such as Claude Code and OpenAI Codex).
When an active agent encounters rate limits or quota boundaries (e.g., 5-hour session limits or weekly caps, classified as `CAPACITY_SESSION` or `CAPACITY_WEEKLY`), `subsched` records the cooldown state along with the target reset timestamp (`reset_at`).
If an alternative enabled agent is configured and available, `subsched` automatically fails over to that agent, allowing task execution to proceed without manual operator intervention.

### 2. What happens if quota or tokens run out in the middle of a task?
Work is never lost or discarded:
- **Worktree & Commit Durability**: Each task runs in its own dedicated Git worktree (`.ai/worktrees/issue-<number>`). Any files created, modifications made, and Git commits recorded prior to interruption remain intact on disk.
- **Semantic Handoff & Mechanical Checkpoints**: The agent continually documents its progress, decisions, and next steps in a structured handoff document (`.ai/handoffs/<issue>.md`). In addition, `subsched` captures mechanical checkpoints of file diffs and commit hashes.
- **State Preservation**: The task transitions to `WAITING_CAPACITY` (or immediately fails over to an alternative agent if one is available).
- **Seamless Resumption**: Once the quota reset time (`reset_at`) arrives or the task is reassigned to an alternative agent, the next worker reads the handoff document and previous commits, resuming work directly from the last state rather than starting over from scratch.

### 3. How are task dependencies handled (e.g., Task B cannot start until Task A completes)?
`subsched` provides dependency tracking and topological execution:
- **Dependency Detection**: Dependencies can be declared in GitHub Issue bodies (e.g., `Blocked by #<number>` or `Depends on #<number>`) or configured explicitly.
- **`WAITING_DEPENDENCY` State**: If Task B depends on Task A, Task B is placed in the `WAITING_DEPENDENCY` state and will not be dispatched while Task A is in progress, verifying, or waiting.
- **Automatic Unblocking**: When Task A passes all verification quality gates and reaches the `COMPLETE` state, `subsched` automatically resolves dependencies, transitions Task B to `READY`, and schedules it for execution in topological order. Circular dependencies are detected and fail-closed to `BLOCKED`.

### 4. Can an external orchestrator (such as Gemini, an LLM controller, or a CI script) assign and monitor tasks?
Yes. `subsched` is built with a deterministic CLI and durable JSON state, making it ideal to be driven by higher-level orchestrators (such as Gemini, autonomous supervisor agents, or CI pipelines):
- **Command & Control**: Orchestrators can drive `subsched` via standard commands: `subsched run --issues <id>` to queue or run specific issues, `subsched pause` / `subsched resume` to control execution flow, and `subsched cancel <id>` to abort specific tasks safely.
- **State & Health Inspection**: The scheduler's state is stored durably in `.ai/scheduler.json`. Orchestrators can query queue status with `subsched status --verbose` or export machine-readable metrics via `subsched metrics --json`.
- **Fail-Closed Escalation for Supervisory AI**: If an unrecoverable event occurs (such as Git rebase merge conflicts, ambiguous existing PR matches, or unexpected agent termination), `subsched` transitions the task to `NEEDS_HUMAN` and records the exact reason in `needs_human_reason`. An external AI orchestrator can inspect this field, triage the root cause, and either remediate the issue programmatically or notify a human operator.

### 5. Are intermediate execution logs and agent transcripts saved?
Yes, execution details are captured and persisted across multiple layers:
- **Structured Event Logs**: All scheduler lifecycle events (`dispatch`, `agent_finish`, `capacity_reset_cleared`, `rebase`, `verification`, `pr_create`, etc.) are written to structured logs with ISO 8601 timestamps, issue numbers, and duration metrics.
- **Agent Process Logs & Transcripts**: Stdout, stderr, and output from native coding agent CLI invocations are captured and recorded in per-task worktree directories and scheduler execution logs.
- **Task Artifacts & Handoffs**: Every worktree retains `.ai/tasks/<issue>.md`, `.ai/handoffs/<issue>.md`, and `.ai/checkpoints/`, documenting incremental progress across worker dispatches and restarts.
- **Markdown Run Reports**: Comprehensive execution summaries (covering productivity, reliability, failure breakdowns, and capacity events) can be generated at any time using `subsched metrics --report run_report.md`.

---

## Documentation

- [`docs/SPEC.md`](docs/SPEC.md): Ground-truth specification and safety invariants.
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md): Operator runbook for running, monitoring, and disaster recovery.
- [`docs/WORKFLOW.md`](docs/WORKFLOW.md): Contributor and development workflow.
