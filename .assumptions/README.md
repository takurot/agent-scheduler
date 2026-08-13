# Phase 0 assumption log

Observed on 2026-08-12. These checks do not establish permanent provider contracts.

| Dependency | Observation | Phase 1 decision |
| --- | --- | --- |
| Python | Python 3.12 is installed through Homebrew and selected by `uv` | Supported |
| GitHub CLI | `gh` is installed; structured issue JSON is supported. Pagination, repo-not-found/invalid-format, and token-scope behavior are recorded in [`github-cli.md`](github-cli.md) (Issue #7 / A4) | Adapter implemented, mocked in tests; `doctor` warns on broad token scope |
| Claude Code | Non-live CLI metadata and replay parser are recorded in [`claude-code.md`](claude-code.md); live behavior remains gated | Native worker disabled |
| Codex CLI | CLI is installed; `codex exec` requires a controlled live spike | Native worker disabled |
| Bernstein | Not installed and compatibility B1-B4 is unverified | Not a runtime dependency |
| ccusage | Not installed; local usage is not provider capacity ground truth | Not used for admission control |

Live Agent calls, subscription consumption, GitHub mutations, and PR creation are deliberately
excluded from automatic Phase 0 tests. Capture raw schemas only after billing/authentication mode
has been confirmed, and never record tokens or credential-bearing environment values.
