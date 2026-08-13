# Codex CLI native spike (Issue #6)

Date: 2026-08-13

## Safe observation

Only non-provider metadata was inspected. `codex --version` reported `codex-cli 0.147.0`, and
`codex exec --help` advertised JSONL output (`--json`), final-response schema validation
(`--output-schema`), an explicit working root (`-C`), `workspace-write` sandboxing, and ephemeral
sessions (`--ephemeral`). It also advertised dangerous approval/sandbox bypass options; the probe
builder deliberately never emits them. The hardened probe fixes approval to `never`, ignores user
configuration and exec-policy rules, and rejects unknown configuration fields. The prompt is sent
on stdin, not placed in argv.

## Controlled live observation

After explicit user opt-in and independently supplied `Logged in using ChatGPT` evidence, preflight
revalidated that authentication method and Codex CLI `0.147.0`. One minimum provider call ran in a
temporary Git repository using the adapter's fixed stdin prompt, output schema, `workspace-write`
sandbox, ephemeral session, allowlisted environment, and 120-second timeout.

This observation predates the later argv hardening that explicitly added `--ask-for-approval
never`, `--ignore-user-config`, `--ignore-rules`, and `--strict-config`. It establishes the success
event shape only; approval and configuration-isolation paths remain synthetic.

The call succeeded with exit code `0`, no stderr, and four JSONL events: `thread.started`,
`turn.started`, an `item.completed` agent message matching the strict final schema, and
`turn.completed`. The parser normalized the stream to `AgentResultKind.PASS`. The raw stream was
deleted with the temporary directory. `live-success.jsonl` retains only event types and the
schema-required final message; the thread ID is replaced with a fixed UUID and usage values are
removed. No prompt, credential, personal path, raw free-form model output, or real usage count is
retained. The exact CLI version and relevant help excerpt are saved beside the fixture.

Authentication, billing, capacity, approval, malformed event, timeout, and process-cleanup error
paths remain synthetic by design. Intentionally exhausting subscription capacity or corrupting
authentication would be unsafe; timeout and process-tree cleanup use local fake process tests.

## Replayable contract

Secret-free JSONL fixtures under `tests/fixtures/codex/` cover live success plus synthetic session and
weekly capacity, authentication, billing, non-interactive approval, unknown reset, and malformed or
unknown events. They normalize to the shared immutable `AgentResult` contract. The matching final
response schema is `tests/fixtures/codex/final-output.schema.json`.

The replay parser is intentionally fail-closed. Unknown event types, malformed JSON/schema,
nonzero exit without a classified structured error, and capacity without a timezone-aware reset
become `FAILURE`; they never become `PASS` or an assumed cooldown.

## Live acceptance gate

Any future provider probe still requires explicit operator opt-in and independent verification that
authentication uses the intended subscription with metered/API fallback disabled. Its output must
be scrubbed of secrets, prompts, issue content, and personal paths before a fixture is committed.
The error fixtures remain synthetic and are not claimed as captured provider responses.
