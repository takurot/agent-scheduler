# Phase 0 assumption log

Observed on 2026-08-12. These checks do not establish permanent provider contracts.

| Dependency | Observation | Phase 1 decision |
| --- | --- | --- |
| Python | Python 3.12 is installed through Homebrew and selected by `uv` | Supported |
| GitHub CLI | `gh` is installed; structured issue JSON is supported | Adapter implemented, mocked in tests |
| Claude Code | Non-live CLI metadata and replay parser are recorded in [`claude-code.md`](claude-code.md); live behavior remains gated | Native worker disabled |
| Codex CLI | CLI is installed; `codex exec` requires a controlled live spike | Native worker disabled |
| Bernstein | Not installed and compatibility B1-B4 is unverified | Not a runtime dependency |
| ccusage | Not installed; local usage is not provider capacity ground truth | Not used for admission control |

Live Agent calls, subscription consumption, GitHub mutations, and PR creation are deliberately
excluded from automatic Phase 0 tests. Capture raw schemas only after billing/authentication mode
has been confirmed, and never record tokens or credential-bearing environment values.

## Manifest contract

Each controlled observation is stored at `.assumptions/<YYYY-MM-DD>/manifest.json` and validated
against [`manifest.schema.json`](manifest.schema.json). A manifest records the CLI name and version,
redacted argv, timezone-aware timestamp, exit code, SHA-256 hash of the observed output schema, and
the `PASS`, `FAIL`, or `UNKNOWN` decision. `verified` records whether the observation was explicitly
checked; a `PASS` with `verified: false` is invalid. Missing or unrecognized evidence therefore stays
`UNKNOWN` rather than being promoted to `PASS`.

Before persistence, values following token, credential, password, API-key, prompt, and prompt-file
flags are replaced with `[REDACTED]`. Personal absolute paths are replaced with
`[REDACTED_PATH]`. Claude and Codex exec positional prompt bodies are also replaced with
`[REDACTED]`. Prompt bodies and raw provider output are not fields in the manifest.

Live probes require the caller to pass an explicit opt-in to `run_live_probe`. The helper accepts
only the allowlisted Phase 0 CLIs, uses an empty environment, a fixed timeout, captured output, and
never runs from the normal test suite. Unit tests inject a fake subprocess runner; integration tests
replay the deterministic JSON fixtures under `tests/fixtures/assumptions/`.
