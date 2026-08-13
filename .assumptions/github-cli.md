# GitHub CLI native spike — Issue #7 [A4]

Observed on 2026-08-13 with GitHub CLI (`gh`) `2.89.0`. Provider/CLI behavior is an external
dependency; these observations are not a permanent compatibility guarantee.

## Authentication and token scope

`gh auth status --json hosts` (no `--show-token`) returns structured, per-host account state
without exposing the token value:

```json
{
  "hosts": {
    "github.com": [
      {
        "state": "success",
        "active": true,
        "host": "github.com",
        "login": "<redacted>",
        "tokenSource": "keyring",
        "scopes": "gist, read:org, repo, workflow",
        "gitProtocol": "https"
      }
    ]
  }
}
```

A sanitized copy is stored at `tests/fixtures/github/auth-status-broad-scope.json`
(login replaced with a placeholder). Classic GitHub OAuth/PAT scopes do not separate
"read issues" from "push / open a PR": the `repo` scope alone grants full read+write on private
repositories, and `public_repo` grants read+write on public ones. There is no scope that grants
issue-list read access without also granting write. Given that, `diagnose_token` in
`subsched/github/issues.py` cannot prove a token is read-only; it instead reports whether any
scope *capable* of mutation (`repo`, `public_repo`, `workflow`, `delete_repo`, `admin:org`,
`admin:repo_hook`) is present, and flags that as broader than Phase 1 discovery needs. `doctor`
prints this diagnosis and warns on broad scopes without ever printing the token itself.

## Pagination and >100 issue behavior

`gh issue list --limit N` (`N` above the default 30) internally paginates the underlying GraphQL
API; the caller only ever supplies one command. Measured live against a public repository with
several thousand open issues:

```console
$ gh issue list --repo microsoft/vscode --state open --limit 130 \
    --json number,title,body,labels,url
# exit 0, stderr empty, 1.75s wall time, 130 items returned
```

This confirms `--limit` — not a client-side loop — controls truncation, and `gh` does not warn
when more matching issues exist beyond the requested limit. `subsched.github.issues.
GitHubIssueSource.list_open` currently hardcodes `--limit 100`; a repository with more than 100
currently-open issues matching the filter will silently miss the remainder. This is recorded as
a known gap and pinned by
`tests/unit/test_github_adapter.py::test_github_adapter_current_limit_argv_caps_at_100`.
Implementing loss-free pagination (e.g. raising the limit, or paging with `--search`/cursor state)
is B4's acceptance criteria, not A4's; A4 only measures and records the behavior.

A synthetic 105-issue fixture (`tests/fixtures/github/issue-list-over-100.json`) exercises that
JSON parsing itself has no separate 100-item ceiling — only the request argv does — via
`test_github_adapter_parses_over_100_issues_without_truncation`.

## repo not found / invalid repo format

Both measured live (read-only; a nonexistent-repo lookup and a malformed `--repo` value cost no
capacity and mutate nothing):

```console
$ gh issue list --repo owner/definitely-nonexistent-repo --json number,title
GraphQL: Could not resolve to a Repository with the name 'owner/definitely-nonexistent-repo'. (repository)
# exit 1

$ gh issue list --repo not-a-valid-format --json number,title
expected the "[HOST/]OWNER/REPO" format, got "not-a-valid-repo-format"
# exit 1
```

Sanitized stderr copies are stored at `tests/fixtures/github/issue-list-repo-not-found.stderr.txt`
and `tests/fixtures/github/issue-list-invalid-repo-format.stderr.txt`. Both are `exit 1`; the
invalid-format case is rejected client-side by `gh` before any network call (fast, no auth
required), while the not-found case requires one GraphQL round trip. `GitHubIssueSource.list_open`
already treats every nonzero exit uniformly as `GitHubCliError("GitHub CLI exited with N; stderr
hidden")`, which is intentionally coarse to avoid leaking provider stderr; this spike does not
change that classification.

## Auth error — synthetic, not reproduced live

Reproducing an unauthenticated `gh` call live would require logging the operator's real session
out, which this spike intentionally avoided. `tests/fixtures/github/
auth-status-unauthenticated.stderr.txt` is a synthetic fixture based on `gh`'s documented
unauthenticated message text, not a captured live transcript. `diagnose_token`'s fail-closed path
for a nonzero `gh auth status` exit code is covered by
`test_diagnose_token_fails_closed_when_unauthenticated`, which drives that path directly rather
than depending on this fixture text matching exactly.

## Private repository and fork — not independently measured

GitHub's GraphQL API is documented to return the same "Could not resolve to a Repository" error
for both a nonexistent repository and a private repository the caller's token cannot see (to
avoid leaking private-repo existence). This spike did not verify that distinction live against a
real private repository or fork, since doing so would require provisioning one. Treat "private
repo (no access)" as behaviorally identical to "repo not found" from the adapter's point of view
until independently verified; this is an open item, not a confirmed fact.

## Read-only discovery vs. push/PR write permission

`GitHubIssueSource` only ever calls `gh issue list` (read). No code path in this repository calls
`gh issue create`, `gh pr create`, or any mutating `gh` subcommand; push/PR write adapters are
future Phase 2+ work (SPEC §72 keeps discovery and write as separate permission tiers). The
`diagnose_token` scope check in this Issue is the mechanism that lets `doctor` warn an operator
before that future work lands, if the credential already in use is broader than today's read-only
need.
