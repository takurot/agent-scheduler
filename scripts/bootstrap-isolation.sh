#!/usr/bin/env bash
set -euo pipefail

# Bootstraps the per-repository Docker resources (internal network, allowlist
# proxy container, dedicated provider auth directories) documented in
# README.md's "Container Sandbox Setup Guide". This script never deletes or
# modifies an existing Docker network, container, or auth directory contents;
# any naming collision is reported and the run fails closed instead of
# guessing which resource is safe to reuse. It never touches subsched.yaml
# (a user-owned file) -- it only prints an `isolation:` YAML fragment for the
# operator to paste in.

usage() {
  cat <<'EOF'
Usage: scripts/bootstrap-isolation.sh <owner/repo> --proxy-image <ref@sha256:...> [options]

Required:
  <owner/repo>              Repository slug used to derive default resource names.
  --proxy-image <ref>       RepoDigest-pinned proxy image (must include @sha256:...).
                             Not required when --check is used alone.

Options:
  --check                   Report the status of the derived resources without
                             creating, modifying, or deleting anything.
  --worker-image <ref>      RepoDigest-pinned worker image to embed in the
                             printed isolation: block (informational only;
                             this script never launches worker containers).
  --language <lang>         Target project language (rust, python, node, etc.)
                             to display toolchain pre-baking recipes and guidance.
  --network-name <name>     Override the derived Docker internal network name.
  --proxy-name <name>       Override the derived proxy container name.
  --auth-base <dir>         Override the derived base directory for dedicated
                             claude/codex provider auth directories.
  -h, --help                Show this help text.

This script never deletes or overwrites an existing Docker network, container,
or auth directory. Any naming collision is reported and the run exits non-zero
instead of guessing which resource is safe to reuse.
EOF
}

repo_slug=""
proxy_image=""
worker_image=""
language=""
network_name=""
proxy_name=""
auth_base=""
check_only=false

while [ "$#" -gt 0 ]; do
  case "$1" in
    --check)
      check_only=true
      shift
      ;;
    --proxy-image)
      proxy_image="${2:-}"
      shift 2
      ;;
    --worker-image)
      worker_image="${2:-}"
      shift 2
      ;;
    --language)
      language="${2:-}"
      shift 2
      ;;
    --network-name)
      network_name="${2:-}"
      shift 2
      ;;
    --proxy-name)
      proxy_name="${2:-}"
      shift 2
      ;;
    --auth-base)
      auth_base="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [ -n "$repo_slug" ]; then
        echo "error: unexpected extra argument: $1" >&2
        exit 1
      fi
      repo_slug="$1"
      shift
      ;;
  esac
done

if [ -z "$repo_slug" ]; then
  echo "error: <owner/repo> is required" >&2
  usage >&2
  exit 1
fi

if ! printf '%s' "$repo_slug" | grep -Eq '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$'; then
  echo "error: <owner/repo> must look like 'owner/name', got: $repo_slug" >&2
  exit 1
fi

if [ "$check_only" = false ] && [ -z "$proxy_image" ]; then
  echo "error: --proxy-image is required unless --check is used" >&2
  exit 1
fi

if [ -n "$proxy_image" ] && ! printf '%s' "$proxy_image" | grep -q '@sha256:'; then
  echo "error: --proxy-image must be pinned by digest (expected '...@sha256:<digest>'), got: $proxy_image" >&2
  exit 1
fi

if [ -n "$worker_image" ] && ! printf '%s' "$worker_image" | grep -q '@sha256:'; then
  echo "error: --worker-image must be pinned by digest (expected '...@sha256:<digest>'), got: $worker_image" >&2
  exit 1
fi

slug_id="$(printf '%s' "$repo_slug" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')"

: "${network_name:=subsched-${slug_id}-internal}"
: "${proxy_name:=subsched-${slug_id}-proxy}"
: "${auth_base:=$HOME/.cache/subsched-bootstrap/${slug_id}}"

claude_auth_dir="${auth_base}/claude"
codex_auth_dir="${auth_base}/codex"

if ! command -v docker >/dev/null 2>&1; then
  echo "error: docker CLI not found on PATH" >&2
  exit 1
fi

network_exists() {
  docker network inspect "$network_name" >/dev/null 2>&1
}

proxy_exists() {
  docker container inspect "$proxy_name" >/dev/null 2>&1
}

dir_status() {
  dir="$1"
  if [ -d "$dir" ]; then
    mode="$(stat -f '%Lp' "$dir" 2>/dev/null || stat -c '%a' "$dir" 2>/dev/null || echo unknown)"
    echo "exists (mode: $mode)"
  else
    echo "missing"
  fi
}

if [ "$check_only" = true ]; then
  echo "=== bootstrap-isolation.sh --check: $repo_slug ==="
  if network_exists; then
    echo "network:      $network_name -> exists"
  else
    echo "network:      $network_name -> missing"
  fi
  if proxy_exists; then
    echo "proxy:        $proxy_name -> exists"
  else
    echo "proxy:        $proxy_name -> missing"
  fi
  echo "auth (claude): $claude_auth_dir -> $(dir_status "$claude_auth_dir")"
  echo "auth (codex):  $codex_auth_dir -> $(dir_status "$codex_auth_dir")"
  exit 0
fi

if network_exists; then
  echo "error: Docker network '$network_name' already exists; refusing to overwrite it." >&2
  echo "       Re-run with --check to inspect it, or pass --network-name to pick a different name." >&2
  exit 1
fi

if proxy_exists; then
  echo "error: Docker container '$proxy_name' already exists; refusing to overwrite it." >&2
  echo "       Re-run with --check to inspect it, or pass --proxy-name to pick a different name." >&2
  exit 1
fi

echo "=== Creating internal network: $network_name ==="
docker network create --internal "$network_name" >/dev/null

echo "=== Starting proxy container: $proxy_name ==="
docker run -d \
  --name "$proxy_name" \
  --network "$network_name" \
  "$proxy_image" >/dev/null

echo "=== Connecting proxy to an outbound network for provider egress ==="
docker network connect bridge "$proxy_name"

echo "=== Preparing dedicated provider auth directories ==="
mkdir -p "$claude_auth_dir" "$codex_auth_dir"
chmod 700 "$claude_auth_dir" "$codex_auth_dir"

cat <<EOF

=== Done. Paste the block below into your repository's subsched.yaml ===

isolation:
  backend: container
  runtime: docker
  image: ${worker_image:-"<registry>/<worker-image>@sha256:<digest>"}
  network: ${network_name}
  proxy_url: http://${proxy_name}:3128
  proxy_image: ${proxy_image}
  auth:
    claude: ${claude_auth_dir}
    codex: ${codex_auth_dir}

Place your Claude/Codex subscription credentials into the auth directories above
(mode 0600 files) before running 'subsched doctor'.
EOF

if [ -n "$language" ]; then
  case "$(printf '%s' "$language" | tr '[:upper:]' '[:lower:]')" in
    rust)
      cat <<'EOFRUST'

=== Toolchain Guidance for Rust (#364) ===
Worker containers execute in a read-only, isolated network environment.
Pre-bake the Rust toolchain (cargo, rustc, rustfmt, clippy) into your worker image
using the reference Dockerfile: examples/docker/Dockerfile.worker-rust

Build and pin your worker image:
  docker build -t <worker-image>:latest -f examples/docker/Dockerfile.worker-rust .
  docker inspect <worker-image>:latest --format '{{index .RepoDigests 0}}'
EOFRUST
      ;;
    python)
      cat <<'EOFPY'

=== Toolchain Guidance for Python (#364) ===
Worker containers execute in a read-only, isolated network environment.
Pre-bake Python tools (uv, pytest, ruff) into your worker image
using the reference Dockerfile: examples/docker/Dockerfile.worker-python
EOFPY
      ;;
    *)
      cat <<EOFLANG

=== Toolchain Guidance for $language (#364) ===
Worker containers execute in a read-only, isolated network environment.
Ensure all compilers, test runners, and linters for $language are pre-baked
into your worker image (see examples/docker/README.md).
EOFLANG
      ;;
  esac
fi

