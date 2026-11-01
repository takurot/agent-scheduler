# Online dependency audit contract (issue #495)

Status: design and executable contract spike, not enabled in Scheduler. No task
state, configuration, gate, or PASS semantics change. Live acceptance is pending;
see the evidence ledger below. `scripts/audit_contract_spike.py` is a pure model,
not an artifact importer, signature verifier, or supported verification command.

## Decision

Select an explicitly independent CI audit for the first operational path. Keep
all repository tests/lint in the existing fresh, digest-pinned, network-none
verification container. A full result requires both that offline result and a
fresh authenticated online audit for exactly the same target. Neither a local
worker claim nor an offline test result supplies the online half.

| Candidate | Benefit | Cost / decision |
| --- | --- | --- |
| Trusted metadata prefetch and offline matching | No network in any repository gate | Requires a complete versioned advisory snapshot, resolver/marker coverage, authenticity and expiry handling, and an offline matcher supported by the pinned tool. An HTTP cache alone does not establish completeness or freshness. Defer. |
| Independent CI audit | Can reuse an existing online CI service and pinned audit tool without a new advisory database | Requires trusted workflow provenance, exact package inventory and freshness checks. Select for the contract spike; automated consumption remains unsupported. |

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

Before dispatch, the operator reviews `verification.commands` and all scripts they
invoke for online requirements; binary/toolchain probes do not prove metadata
availability. The current Scheduler cannot import this independent CI evidence or
combine it with offline gates. Consequently a full gate containing online
`pip-audit` in network-none VERIFYING is an unsupported verification plan: explain
that missing capability and stop before dispatch. Do not dispatch repeatedly in the
hope that DNS succeeds. Script names are not a reliable generic online detector;
this planning step is explicitly operator-owned in this design stage.

Do not configure just the offline half as a full Scheduler gate and allow PR
creation. Do not enable push/PR completion for an unsupported split plan. If a task
is already in this situation, preserve its worktree/handoff and use operator
intervention rather than asking the worker to change network or remove the audit.
Manual inspection of independent CI can inform review but does not transition a
Scheduler task or make its VERIFYING result PASS. Automated adoption requires a
separately reviewed authenticated CI reader and composite gate, with pre-dispatch
admission and failure classification; this spike deliberately does not add them.

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

At implementation time Docker is not installed in the task environment. Neither
live record has been produced by this spike. The issue's historical reproduction
is context, not new acceptance evidence. Unit success, skipped Docker tests and a
local full gate cannot substitute for these two records. An operator with the
attested Docker host and trusted CI producer must supply them before this design
can be considered validated for adoption.
