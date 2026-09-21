#!/usr/bin/env bash
set -euo pipefail

# Run only on an operator-controlled Docker host. No provider credentials are used.
cd "$(dirname "$0")/.."

for name in SUBSCHED_ISOLATION_WORKER_IMAGE SUBSCHED_ISOLATION_PROXY_IMAGE; do
  if [ -z "${!name:-}" ]; then
    echo "error: $name is required" >&2
    exit 1
  fi
  if ! [[ "${!name}" =~ @sha256:[a-f0-9]{64}$ ]]; then
    echo "error: $name must be a digest-pinned image" >&2
    exit 1
  fi
done

if [ -z "${SUBSCHED_ISOLATION_REPORT:-}" ]; then
  echo "error: SUBSCHED_ISOLATION_REPORT is required" >&2
  exit 1
fi
if [ -e "$SUBSCHED_ISOLATION_REPORT" ] || [ -L "$SUBSCHED_ISOLATION_REPORT" ]; then
  echo "error: report path already exists" >&2
  exit 1
fi
command -v docker >/dev/null || { echo "error: docker is required" >&2; exit 1; }
command -v uv >/dev/null || { echo "error: uv is required" >&2; exit 1; }

# Verify the exact local image identities before creating any Docker resources.
for image in "$SUBSCHED_ISOLATION_WORKER_IMAGE" "$SUBSCHED_ISOLATION_PROXY_IMAGE"; do
  if ! docker image inspect "$image" --format '{{json .RepoDigests}}' | grep -Fq "\"$image\""; then
    echo "error: configured digest is unavailable locally" >&2
    exit 1
  fi
done

run_dir="$(mktemp -d "${TMPDIR:-/tmp}/subsched-live-isolation.XXXXXXXX")"
chmod 700 "$run_dir"
run_name="subsched-live-$(basename "$run_dir" | tr -cd 'a-zA-Z0-9' | tr '[:upper:]' '[:lower:]' | cut -c1-24)"
network_id=""
proxy_id=""
result="failed"
cleanup_result="passed"
docker_version="$(docker info --format '{{.ServerVersion}}')"
commit_sha="$(git rev-parse HEAD)"

finish() {
  exit_code=$?
  trap - EXIT
  if [ -n "$proxy_id" ]; then
    docker container rm --force "$proxy_id" >/dev/null || cleanup_result="failed"
  fi
  if [ -n "$network_id" ]; then
    docker network rm "$network_id" >/dev/null || cleanup_result="failed"
  fi
  if [ "$cleanup_result" = "failed" ]; then
    exit_code=1
  fi
  if [ "$exit_code" -eq 0 ]; then
    result="passed"
  fi
  REPORT_PATH="$SUBSCHED_ISOLATION_REPORT" \
  REPORT_RESULT="$result" REPORT_CLEANUP="$cleanup_result" \
  REPORT_COMMIT="$commit_sha" REPORT_DOCKER="$docker_version" \
  REPORT_WORKER="$SUBSCHED_ISOLATION_WORKER_IMAGE" \
  REPORT_PROXY="$SUBSCHED_ISOLATION_PROXY_IMAGE" \
  REPORT_NETWORK="$run_name" \
    uv run --no-sync python - <<'PY'
import json
import os

report = {
    "commit": os.environ["REPORT_COMMIT"],
    "docker_version": os.environ["REPORT_DOCKER"],
    "worker_image": os.environ["REPORT_WORKER"],
    "proxy_image": os.environ["REPORT_PROXY"],
    "network": os.environ["REPORT_NETWORK"],
    "proxy_url": "http://<ephemeral-proxy>:3128",
    "tests": "tests/integration/test_native_isolation_container.py (4 required)",
    "result": os.environ["REPORT_RESULT"],
    "cleanup": os.environ["REPORT_CLEANUP"],
}
fd = os.open(os.environ["REPORT_PATH"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as handle:
    json.dump(report, handle, sort_keys=True)
    handle.write("\n")
PY
  report_exit=$?
  rm -rf -- "$run_dir"
  if [ "$report_exit" -ne 0 ]; then
    exit_code=1
  fi
  exit "$exit_code"
}
trap finish EXIT

mkdir -m 700 "$run_dir/auth"
printf 'synthetic-provider-auth\n' > "$run_dir/auth/auth.json"
chmod 600 "$run_dir/auth/auth.json"

network_id="$(docker network create --internal "$run_name")"
proxy_id="$(docker run -d --name "$run_name-proxy" --network bridge \
  --read-only --cap-drop ALL --security-opt no-new-privileges=true --ipc private \
  "$SUBSCHED_ISOLATION_PROXY_IMAGE")"
docker network connect "$network_id" "$proxy_id"

export SUBSCHED_DOCKER_ISOLATION_TEST=1
export SUBSCHED_ISOLATION_NETWORK="$run_name"
export SUBSCHED_ISOLATION_PROXY_URL="http://$run_name-proxy:3128"
uv sync --frozen
if ! pytest_output="$(uv run pytest -q -ra tests/integration/test_native_isolation_container.py 2>&1)"; then
  printf '%s\n' "$pytest_output"
  exit 1
fi
printf '%s\n' "$pytest_output"
if ! printf '%s\n' "$pytest_output" | grep -Eq '^4 passed in '; then
  echo "error: all four live isolation tests must pass without skips" >&2
  exit 1
fi
