# Codex CLI native spike (Issue #6)

Date: 2026-08-13

## Safe observation

Only non-provider metadata was inspected. `codex --version` reported `codex-cli 0.147.0`, and
`codex exec --help` advertised JSONL output (`--json`), final-response schema validation
(`--output-schema`), an explicit working root (`-C`), `workspace-write` sandboxing, and ephemeral
sessions (`--ephemeral`). It also advertised dangerous approval/sandbox bypass options; the probe
builder deliberately never emits them. The prompt is sent on stdin, not placed in argv.

No provider prompt was executed. Authentication mode, subscription billing mode, successful live
event shapes, approval failure shape, capacity failure/reset shape, and real process-tree cleanup
therefore remain unverified.

## Replayable contract

Synthetic, secret-free JSONL fixtures under `tests/fixtures/codex/` cover success, session and
weekly capacity, authentication, billing, non-interactive approval, unknown reset, and malformed or
unknown events. They normalize to the shared immutable `AgentResult` contract. The matching final
response schema is `tests/fixtures/codex/final-output.schema.json`.

The replay parser is intentionally fail-closed. Unknown event types, malformed JSON/schema,
nonzero exit without a classified structured error, and capacity without a timezone-aware reset
become `FAILURE`; they never become `PASS` or an assumed cooldown.

## Live acceptance gate

A future minimal provider probe may run only after both explicit operator opt-in and independent
verification that authentication uses the intended subscription with metered/API fallback disabled.
Its output must be scrubbed of secrets, prompts, issue content, and personal paths before a fixture
is committed. Until then, this spike does not claim that any synthetic provider event matches a
live Codex response.
