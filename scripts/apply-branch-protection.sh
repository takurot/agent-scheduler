#!/usr/bin/env bash
# Applies (or dry-run previews) the default-branch ruleset defined in
# .github/branch-protection/main.json, so the CI "verify" check is enforced
# as a required merge gate (issue #192, #226).
#
# This performs a permission change on the GitHub repository and must be run
# manually by a maintainer with admin access; it is never invoked automatically.
#
# Usage:
#   scripts/apply-branch-protection.sh                     # dry-run: preview payload & target
#   scripts/apply-branch-protection.sh --apply             # actually create/update the ruleset
#   scripts/apply-branch-protection.sh --ruleset-id 20771505 # target specific existing ruleset ID
#   scripts/apply-branch-protection.sh --repo owner/repo   # target specific repo
set -euo pipefail

ruleset_file="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.github/branch-protection/main.json"
ruleset_name="main-required-checks"
apply=false
ruleset_id="${RULESET_ID:-}"
repo_slug="${REPO_SLUG:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --apply)
      apply=true
      shift
      ;;
    --ruleset-id)
      test -n "${2-}" || { echo "Missing argument for --ruleset-id" >&2; exit 1; }
      ruleset_id="$2"
      shift 2
      ;;
    --ruleset-id=*)
      ruleset_id="${1#*=}"
      shift
      ;;
    --repo)
      test -n "${2-}" || { echo "Missing argument for --repo" >&2; exit 1; }
      repo_slug="$2"
      shift 2
      ;;
    --repo=*)
      repo_slug="${1#*=}"
      shift
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 1
      ;;
  esac
done

test -f "$ruleset_file" || { echo "Ruleset definition not found: $ruleset_file" >&2; exit 1; }

# Dynamically resolve repo_slug if not explicitly provided
if [ -z "$repo_slug" ]; then
  repo_slug="$(gh repo view --json nameWithOwner --jq .nameWithOwner 2>/dev/null || true)"
fi
test -n "$repo_slug" || {
  echo "Failed to resolve repository via gh; please specify with --repo <owner/repo>" >&2
  exit 1
}

default_branch="$(gh repo view "$repo_slug" --json defaultBranchRef --jq .defaultBranchRef.name 2>/dev/null || true)"
test -n "$default_branch" || { echo "Failed to resolve default branch for $repo_slug; aborting." >&2; exit 1; }

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

existing_id=""
if [ -n "$ruleset_id" ]; then
  existing_id="$ruleset_id"
else
  # 1. Search by ruleset name
  existing_id="$(gh api "repos/$repo_slug/rulesets" --jq \
    ".[] | select(.name == \"$ruleset_name\") | .id" 2>/dev/null || true)"

  # 2. Fallback: if not found by name, check for an existing default branch ruleset to update
  if [ -z "$existing_id" ]; then
    candidate_ids="$(gh api "repos/$repo_slug/rulesets" --jq \
      '.[] | select(.target == "branch") | .id' 2>/dev/null || true)"
    count="$(printf '%s\n' "$candidate_ids" | grep -v '^$' | wc -l | tr -d ' ')"
    if [ "$count" -eq 1 ]; then
      existing_id="$candidate_ids"
      echo "Note: Detected single existing branch ruleset (id=$existing_id); will target for update." >&2
    fi
  fi
fi

if [ "$apply" = false ]; then
  echo "Dry run. Target repo: $repo_slug ($default_branch)"
  if [ -n "$existing_id" ]; then
    echo "Action: Would update existing ruleset (PUT repos/$repo_slug/rulesets/$existing_id) with payload:"
  else
    echo "Action: Would create new ruleset (POST repos/$repo_slug/rulesets) with payload:"
  fi
  echo "$payload" | python3 -m json.tool
  echo
  echo "Re-run with --apply to make this change."
  exit 0
fi

if [ -n "$existing_id" ]; then
  echo "Updating existing ruleset id=$existing_id on $repo_slug..."
  echo "$payload" | gh api --method PUT "repos/$repo_slug/rulesets/$existing_id" --input -
else
  echo "Creating new ruleset on $repo_slug..."
  echo "$payload" | gh api --method POST "repos/$repo_slug/rulesets" --input -
fi

echo "Ruleset applied to $repo_slug ($default_branch)."
