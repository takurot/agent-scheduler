# Claude Code native spike — Issue #5

Observed on 2026-08-13 with Claude Code `2.1.229`. Provider behavior is an external dependency;
these observations are not a permanent compatibility guarantee.

## Safe local metadata observation

Only `claude --version` and `claude --help` were executed. No prompt, authentication probe, remote
request, or provider-capacity probe was run.

- `--print` supports non-interactive execution.
- `--output-format` advertises `json` and `stream-json`.
- `--json-schema` advertises validated structured output.
- `--permission-mode` advertises `dontAsk` and other modes.
- `--no-session-persistence` and `--strict-mcp-config` are available for isolation.
- `--dangerously-skip-permissions` is available but is unsafe for this Scheduler and must not be
  selected.
- No `--max-turns` option is advertised by this version. The adapter contract therefore requires
  the Scheduler's wall-clock timeout and process-group cleanup; absence of successful cleanup is a
  separate failure classification.
- No CLI sandbox flag is advertised. Permission mode is not treated as proof of OS sandboxing, so
  native execution remains disabled until the Phase 2 process/environment/worktree isolation gate
  is implemented and verified.

The sanitized relevant help/version excerpts are stored under `tests/fixtures/claude/`. They omit
machine paths and contain no credentials.

## Replay contract

`subsched.agents.claude.parse_claude_result` accepts a bounded captured process outcome and
normalizes it to `AgentResult`. Tests replay deterministic, sanitized JSON/text fixtures for:

- ordinary and schema-structured success
- session, weekly, and temporary capacity failures
- authentication, billing, permission, generic execution, and unknown failures
- Scheduler timeout and process-group cleanup failure

Raw Agent output is not copied into the normalized result. Invalid JSON, unknown schemas, and a
capacity response without a timezone-aware reset timestamp are classified as `UNKNOWN`.

The success and failure JSON files are contract fixtures, not evidence from a live subscription
call. Their schema and classification must be revalidated in a controlled live acceptance run
before enabling a native worker.

## Billing and live acceptance gate

Live prompts are denied unless both conditions are explicitly supplied:

1. a live-probe opt-in for this run; and
2. verified subscription-only billing.

`UNKNOWN_BILLING` normalizes to an `AgentResult` and maps the worker to `DISABLED_BILLING`.
Metered billing is also disabled. Neither state may fall back to API usage.

## Controlled live observation — 2026-08-13

After explicit user opt-in and independently supplied evidence of `claude.ai` Pro login, preflight
revalidated first-party Pro authentication and rejected API-key, custom gateway, Bedrock, and
Vertex environment overrides. One minimal provider call ran in a temporary Git repository with
safe mode, no tools, plan permission mode, no MCP servers, no session persistence, a strict
one-field output schema, and a 120-second outer timeout.

The call reached the provider but did not produce a successful result: exit code `1`, JSON
`type=result`, `subtype=success`, `is_error=true`, and `num_turns=1`. This contradictory combination
is replayed by `live-inconsistent-result.json` and classified fail-closed as `UNKNOWN`. The raw
response was deleted with the temporary directory. Session/UUID, usage, cost, model, duration,
terminal reason, provider text, prompt, and personal paths were not retained. The one-call
authorization was respected and no automatic retry occurred.

Still unverified because the authorized call did not succeed:

- actual successful JSON emitted by the authenticated subscription
- provider capacity and reset-time messages
- authentication and billing failure text/schema
- tool permission behavior within the intended OS sandbox
- real timeout signal handling and descendant-process cleanup
