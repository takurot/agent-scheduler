Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No speculative error handling for truly internal impossible states. External input, persisted
  state, filesystem, subprocess, authentication, billing, capacity, and security boundaries always
  require explicit validation and fail-closed handling.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. Project Workflow & Invariants (`docs/WORKFLOW.md`)

All development in this repository must strictly adhere to [`docs/WORKFLOW.md`](docs/WORKFLOW.md) and [`docs/SPEC.md`](docs/SPEC.md).

### Source of Truth & Core Invariants (`docs/WORKFLOW.md` §1, §5)
- **Subscription-only**: Never enable metered usage, token purchasing, or API fallback.
- **Task Isolation**: 1 Issue = 1 Task = 1 branch = 1 worktree. Work only on the designated issue.
- **Worktree Preservation**: Never execute `git reset --hard` or broad `git clean`. Preserve uncommitted changes, untracked files, and prior agent work.
- **Fail-Closed**: If billing, authentication, capacity, schema, or path boundaries are ambiguous or unknown, fail closed immediately.
- **Security**: Prevent path traversal, reject symlinked task/handoff files, and never persist credentials or secrets to logs or fixtures.

### Branch Lifecycle & Synchronization (`docs/WORKFLOW.md` §3)
- Fast-forward sync with `main` before starting: `git switch main && git pull --ff-only`.
- Development branches follow: `issue/<ISSUE>-<short-description>`.
- The `subsched/issue-N` branch pattern is reserved exclusively for Scheduler-managed task worktrees.

### Testing & Quality Gate (`docs/WORKFLOW.md` §4, §7)
- **TDD Workflow**:
  1. Write failing tests first. Do NOT implement yet.
  2. Run tests, confirm they fail for the right reason.
  3. Implement the minimal code to make tests pass.
  4. Do NOT modify tests to make them pass — fix the implementation.
- **Iterative Testing**:
  - Scoped tests: `uv run pytest <path>`
  - Type checking: `uv run mypy src`
- **Pre-Commit Quality Gate**: Run the full gate before committing:
  ```bash
  bash scripts/quality_gate.sh
  ```
  (Executes `ruff check .`, `mypy src`, `pytest` with `--cov-fail-under=80`, `scripts/check_branch_coverage.py` requiring >=80% branch coverage, and `pip-audit`). Never report a task complete without running and displaying the gate output.

### Documentation Synchronization (`docs/WORKFLOW.md` §8)
- When modifying CLI commands, configuration options, task states/transitions, or schemas, update `README.md`, `docs/SPEC.md`, `docs/WORKFLOW.md`, or `examples/scheduler.yaml` within the **same PR**.

### Commits & Pull Requests (`docs/WORKFLOW.md` §9)
- Use Conventional Commits (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `ci:`, `chore:`, `perf:`).
- **Prohibition of Auto-Close Keywords**: Never include GitHub auto-close keywords (`Fixes #N`, `Closes #N`, `Resolves #N`, any case or inflection) in commit messages. Use `issue #N` or `Implements work for #N`.
- Execute the push safety check script in `docs/WORKFLOW.md` §9 before pushing to origin.
- PRs must document the issue reference, changes, rationale, verified test commands, and invariant impact.

### Semantic Handoffs (`docs/WORKFLOW.md` §5, `src/subsched/contract.py`)
- When maintaining `.ai/handoffs/<issue>.md`, preserve all 8 required section headers:
  `## Goal`, `## Current Plan`, `## Completed`, `## Current Work`, `## Decisions`, `## Known Broken State`, `## Next Action`, `## Timestamp`.
- In `## Timestamp`, write ONLY an ISO 8601 string (e.g. `2026-09-11T09:00:00Z`).
