# Online dependency audit contract (issue #495)

Status: issue #526 adds an opt-in CI evidence reader and offline cache handoff.
The real GitHub Actions producer and end-to-end provenance still require live
validation before this path can be treated as production-validated. The original
`scripts/audit_contract_spike.py` remains a model, not a supported importer.

## Decision

Select an explicitly independent CI audit for the first operational path. Keep
all repository tests/lint in the existing fresh, digest-pinned, network-none
verification container. A full result requires both that offline result and a
fresh authenticated online audit for exactly the same target. Neither a local
worker claim nor an offline test result supplies the online half.

| Candidate | Benefit | Cost / decision |
| --- | --- | --- |
| Trusted metadata prefetch and offline matching | No network in any repository gate | Requires a complete versioned advisory snapshot, resolver/marker coverage, authenticity and expiry handling, and an offline matcher supported by the pinned tool. An HTTP cache alone does not establish completeness or freshness. Defer. |
| Independent CI audit | Can reuse an existing online CI service and pinned audit tool without a new advisory database | Requires trusted workflow provenance, exact package inventory and freshness checks. Selected; the optional implementation requires independent CI evidence. |

Do not add PyPI to the provider proxy, give general network to verification, run
modified repository code on the host, or make the final audit optional in
`scripts/quality_gate.sh`. No new dependency or audit-ignore policy is introduced.

## Actors and evidence

The operator controls the audit policy and an immutable CI workflow definition
outside worker authority. CI runs on an ephemeral runner without provider auth,
host HOME, Docker socket, Scheduler state or write credentials. Its online job
runs only trusted audit tooling over a data-only inventory: it must not install
the target project, build source distributions, execute dependency hooks, or
import target repository code. Changes to the workflow/policy require separate
operator review; a workflow copied from the candidate branch cannot attest itself.
The existing repository CI matrix is useful corroboration, but its green badge
alone is not this provenance/freshness contract.

The trusted producer resolves the frozen lock for the declared Python/platform/
extras/groups and records every audited distribution (normalized name, exact
version), including audit/dev dependencies when required by policy. Canonicalize
the inventory as UTF-8 JSON, sorted by normalized name/version, compact separators,
no trailing newline; reject duplicate names or unresolved/unsupported dependencies.
`packages_sha256` hashes those bytes. `lock_sha256` hashes the raw lock bytes.
Policy identifies the complete environment matrix, package scope, advisory service,
no-ignore behavior, canonicalization rules and freshness limit. The tool field
contains its exact pinned version; the workflow digest identifies the trusted
workflow definition. This binding prevents a clean audit of a reduced package set
from being reused for the full environment.

The producer records schema 1 with exactly these fields:

- `target`: repository identity, full 40-character commit SHA, `lock_sha256`,
  `packages_sha256`, exact `tool`, operator-approved `policy`, `workflow_sha256`.
- `observed_at`: timezone-aware acquisition time of fresh advisory information,
  not artifact upload or download time. An old cache cannot reset this timestamp.
- `outcome`: `clean`, `advisory`, or `unavailable`. Only `clean` can satisfy audit.
- `schema`: integer 1.

Record immutable CI run ID and attempt, CI origin/workflow identity, tool version,
policy, acquisition time, package inventory and raw audit report in the independent
CI control plane. Authenticate this association and the SHA-256 of the exact record
bytes through that control plane before classification. A SHA-256 written beside
an artifact by the worker provides no authenticity. Neither does a repository path,
artifact filename, branch name or matching check name. Deny missing/unknown
provenance, unauthorized workflow, cancelled/skipped/pending runs, any failed
required matrix job, unbounded records or unknown schemas. Avoid persisting secrets
or raw provider output. Do not interpret evidence fields as argv, paths or env.

The consumer independently derives the expected commit, raw lock digest, complete
inventory, tool, policy and trusted workflow digest. Match every target field.
Accept acquisition age only in `[0, 24 hours)` using a trusted timezone-aware clock;
future acquisition and exactly 24 hours are rejected. Policy changes, rebase, lock
changes and any new commit invalidate evidence even if package versions coincide.
A valid report with an advisory blocks approval; unavailable metadata, expired or
missing reports require a new audit, never an implementation retry or suppression.

## Planning and current operating limits

The optional implementation derives the target commit, lock, and package inventory
independently in the pinned, network-none image. An operator-approved workflow
runs the data-only audit on a fresh ephemeral runner, and the host verifies the
GitHub run, artifact digest, exact target, inventory and freshness before mounting
its cache read-only in the verification container. The unchanged quality gate still
runs `pip-audit`. A missing, untrusted, stale, or mismatched artifact cannot pass.
The host rechecks evidence and the clean target after the gate. The trusted workflow
is checked out at its operator-approved commit; it must never import or install the
candidate project. The operator controls the allowed workflow commit and digest.

The current path requires the operator to prepare a new run and artifact for each
exact target commit; it does not automatically dispatch or refresh evidence after
worker changes. A missing artifact requires operator intervention, not a worker
retry. A workflow_dispatch workflow must first exist on the default branch, so a
feature branch alone cannot establish genuine GitHub producer evidence. Real
GitHub provenance and both ordinary and linked-worktree acceptance remain required
before asserting end-to-end validation. See [RUNBOOK.md](RUNBOOK.md#online-audit-planning-issue-495).

## Spike and acceptance evidence ledger

Run the deterministic contract fixtures:

```bash
uv run --frozen pytest tests/unit/test_audit_contract_spike.py -q
uv run --frozen mypy scripts/audit_contract_spike.py
```

Fixtures supply a synthetic trusted digest explicitly; they prove classification,
not CI provenance, advisory retrieval, or a successful real online audit. They cover
clean/advisory/unavailable, absent/stale/future records, every changed target binding,
tampering, duplicate fields, malformed schema and unknown expected targets.

Two separate live records are required before claiming issue acceptance:

1. Operator runs the installed audit via `run_verification` in the attested real
   Docker image with network none. Record exact commit, lock/inventory and tool,
   Docker/image digest, sanitized DNS/connection failure and confirmed cleanup.
   Also run ordinary test/lint successfully in the same boundary. No credentials,
   provider calls or permission relaxation are needed.
2. The independently trusted CI producer audits the identical inventory online.
   Record its authenticated origin, immutable run/attempt, trusted workflow digest,
   policy, tool, acquisition time, clean report and record digest. Evaluate against
   independently derived expected target; record classification `clean`. Exercise
   advisory, missing/stale and changed-lock fixtures separately from this live run.

The original design spike did not produce these live records. Subsequent local
Docker and simulated GitHub-transport results must be distinguished from a real
authenticated GitHub Actions run; the latter remains an acceptance requirement.
