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

## 5. Testing

- Iterate with a scoped run during development: `uv run pytest <path>` (a specific file or
  directory, not the full suite).
- Type check after any code change: `uv run mypy src`.
- Before commit, run the full gate from `docs/WORKFLOW.md` §7 (`ruff check .`, `mypy src`,
  `pytest --cov=subsched --cov-fail-under=80`) — see that section for the exact commands and the
  branch-coverage baseline.

### TDD workflow (required for new features)

1. Write failing tests first. Do NOT implement yet.
2. Run tests, confirm they fail for the right reason.
3. Implement the minimal code to make tests pass.
4. Do NOT modify tests to make them pass — fix the implementation.
5. Run the full test command again before reporting done.
6. If a test fails for an unrelated reason, stop and report — do not edit unrelated files.

### Verification is required

- Never report a task as complete without running the relevant test command and showing output.
- Do not rely on your own summary as proof — the command output is the source of truth.
