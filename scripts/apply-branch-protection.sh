#!/usr/bin/env bash
# Applies (or dry-run previews) the default-branch ruleset defined in
# .github/branch-protection/main.json, so the CI "verify" check is enforced
# as a required merge gate (issue #192).
#
# This performs a permission change on the GitHub repository and must be run
# manually by a maintainer with admin access; it is never invoked automatically.
#
# Usage:
#   scripts/apply-branch-protection.sh          # dry-run: print the diff only
#   scripts/apply-branch-protection.sh --apply   # actually create/update the ruleset
set -euo pipefail

repo_slug="takurot/agent-scheduler"
ruleset_file="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.github/branch-protection/main.json"
ruleset_name="main-required-checks"
apply=false

for arg in "$@"; do
  case "$arg" in
    --apply) apply=true ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

test -f "$ruleset_file" || { echo "Ruleset definition not found: $ruleset_file" >&2; exit 1; }

resolved_repo="$(gh repo view "$repo_slug" --json nameWithOwner --jq .nameWithOwner)" || {
  echo "Failed to resolve repository via gh; aborting." >&2
  exit 1
}
test "$resolved_repo" = "$repo_slug" || {
  echo "Resolved repository ($resolved_repo) does not match expected ($repo_slug); aborting." >&2
  exit 1
}

default_branch="$(gh repo view "$repo_slug" --json defaultBranchRef --jq .defaultBranchRef.name)" || {
  echo "Failed to resolve default branch; aborting." >&2
  exit 1
}
test -n "$default_branch" || { echo "Default branch is empty; aborting." >&2; exit 1; }

# Substitute the default branch into the ref_name condition; GitHub's own
# "~DEFAULT_BRANCH" alias would also work, but we resolve it explicitly so the
# payload we print/send is unambiguous.
payload="$(python3 - "$ruleset_file" "$default_branch" <<'PY'
import json
import sys

ruleset_file, default_branch = sys.argv[1], sys.argv[2]
with open(ruleset_file, encoding="utf-8") as f:
    ruleset = json.load(f)

include = ruleset["conditions"]["ref_name"]["include"]
ruleset["conditions"]["ref_name"]["include"] = [
    f"refs/heads/{default_branch}" if ref == "~DEFAULT_BRANCH" else ref for ref in include
]
print(json.dumps(ruleset))
PY
)"

existing_id="$(gh api "repos/$repo_slug/rulesets" --jq \
  ".[] | select(.name == \"$ruleset_name\") | .id" 2>/dev/null || true)"

if [ "$apply" = false ]; then
  echo "Dry run. Would $( [ -n "$existing_id" ] && echo "update ruleset id=$existing_id" || echo "create new ruleset" ) with:"
  echo "$payload" | python3 -m json.tool
  echo
  echo "Re-run with --apply to make this change."
  exit 0
fi

if [ -n "$existing_id" ]; then
  echo "$payload" | gh api --method PUT "repos/$repo_slug/rulesets/$existing_id" --input -
else
  echo "$payload" | gh api --method POST "repos/$repo_slug/rulesets" --input -
fi

echo "Ruleset '$ruleset_name' applied to $repo_slug ($default_branch)."
