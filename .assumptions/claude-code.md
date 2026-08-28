# Claude Code native spike — Issue #5

Observed on 2026-08-13 with Claude Code `2.1.229`. Provider behavior is an external dependency;
these observations are not a permanent compatibility guarantee.

## Initial safe local metadata observation

The spike began by executing only `claude --version` and `claude --help`. No prompt or remote
request was made during this initial metadata step; the separately authorized observations are
recorded below.

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

- ordinary JSON, JSONL (`stream-json`), and schema-structured success
- session, weekly, and temporary capacity failures
- authentication, billing, permission, generic execution, and unknown failures
- Scheduler timeout and process-group cleanup failure

Raw Agent output is not copied into the normalized result. Invalid JSON, unknown schemas, and a
capacity response without a timezone-aware reset timestamp are classified as `UNKNOWN`.

The committed fixtures are sanitized replay contracts. JSON structured success was revalidated in
a controlled subscription call. JSONL, capacity, authentication, billing, permission denial, and
generic failure fixtures remain synthetic so error conditions are not deliberately induced against
the provider.

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

After separate authorization, one retry-free rerun used the same first-party Pro, isolation,
no-tools, MCP, persistence, schema, and timeout controls. It completed in 4,839 ms with exit code
`0`, `type=result`, `subtype=success`, `is_error=false`, `terminal_reason=completed`, no API error or
permission denial, and structured output `{"status":"ok"}`. The sanitized diagnostic files used
for analysis were later deleted at the user's request; raw stdout/stderr were never persisted.

## Permission, sandbox, and timeout boundary

The installed CLI advertises permission mode, safe mode, and the ability to disable tools, but no
verified OS sandbox interface. Permission mode is therefore explicitly not treated as an OS
sandbox, and `native_execution_allowed` remains false. Permission-denial parsing is replayed with a
synthetic structured fixture rather than provoking a live tool call.

A local fake CLI spawns a descendant, ignores SIGTERM in both processes, and verifies the bounded
process-group cleanup helper escalates to SIGKILL and reaps the group. Cleanup confirmation defaults
to unknown; only an explicit successful cleanup result normalizes to an ordinary Scheduler timeout.

Still unverified against the live provider by design:

- provider capacity and reset-time messages
- authentication and billing failure text/schema
- tool permission behavior inside a future verified OS sandbox
- provider-specific behavior during a real timeout (local signal/descendant cleanup is verified)

Consequently, `parse_claude_capacity` is a replay-only contract. The production sensor must not
label policy-derived availability as provider/high or use it to release a saved cooldown until a
separately authorized Phase 0 observation establishes a stable capacity command and schema.
