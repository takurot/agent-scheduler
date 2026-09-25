# Worker Container Images for subsched Isolation

When running `subsched` with container isolation (`isolation.backend: container`), worker containers execute with:
- A read-only root filesystem (`--read-only`)
- An internal Docker network (`Internal: true`) without direct internet access
- Strict proxy allowlisting permitting only verified provider subscription endpoints

Because the container cannot access package repositories or toolchain installers at runtime, **all compilers, language runtimes, linters, and test runners needed by your repository must be pre-baked into the worker container image** (#364).

## Reference Dockerfiles

This directory provides reference Dockerfiles for common project languages:

- [`Dockerfile.proxy`](Dockerfile.proxy) with [`squid.conf`](squid.conf): Squid HTTPS
  tunnel restricted to the Claude and Codex ChatGPT subscription endpoints. It deliberately
  does not permit the metered OpenAI API endpoint.
- [`Dockerfile.worker-rust`](Dockerfile.worker-rust): Debian bookworm base with Node.js, Claude Code/Codex CLIs, `procps`, and full Rust toolchain (`cargo`, `rustc`, `rustfmt`, `clippy`).
- [`Dockerfile.worker-python`](Dockerfile.worker-python): Debian bookworm base with Node.js, Claude Code/Codex CLIs, `procps`, Python 3, and `uv`.

## Updating pinned CLI and Node.js versions

`Dockerfile.worker-python` and `Dockerfile.worker-rust` pin `@anthropic-ai/claude-code@`,
`@openai/codex@`, and the NodeSource `setup_22.x` Node.js LTS line so that rebuilding an image
installs the exact same tool versions every time. To bump a pinned version:

1. Verify the new CLI version against `.assumptions/claude-code.md` or
   `.assumptions/codex-cli.md` (or record a new controlled observation there) before pinning it.
2. Update the `npm install -g @anthropic-ai/claude-code@<version> @openai/codex@<version>` line in
   both `Dockerfile.worker-python` and `Dockerfile.worker-rust`.
3. When the pinned Node.js line approaches its end-of-life date, switch the NodeSource setup
   script (e.g. `setup_22.x` -> `setup_24.x`) to the next active or maintenance LTS line in both
   Dockerfiles; never pin to a Node.js release that has already reached EOL.
4. Rebuild both images and re-run the verification sanity check (`RUN` line at the end of each
   Dockerfile) to confirm the pinned versions install cleanly, then re-obtain and reconfigure the
   immutable `sha256` digest as described below.

## Building and Pinning Images

### 1. Build the image
```bash
# For a Rust project:
docker build -t my-registry/subsched-worker-rust:v1 -f examples/docker/Dockerfile.worker-rust .

# For a Python project:
docker build -t my-registry/subsched-worker-python:v1 -f examples/docker/Dockerfile.worker-python .

# For the allowlist proxy:
docker build -t my-registry/subsched-proxy:v1 -f examples/docker/Dockerfile.proxy .
```

### 2. Push to your container registry (or keep local)
```bash
docker push my-registry/subsched-worker-rust:v1
docker push my-registry/subsched-proxy:v1
```

### 3. Obtain the immutable RepoDigest
Container isolation requires pinning images by immutable sha256 digest (`RepoDigests`):
```bash
docker inspect my-registry/subsched-worker-rust:v1 --format '{{index .RepoDigests 0}}'
# Example output: my-registry/subsched-worker-rust@sha256:abcd...

docker inspect my-registry/subsched-proxy:v1 --format '{{index .RepoDigests 0}}'
```

### 4. Configure `subsched.yaml`
```yaml
isolation:
  backend: container
  runtime: docker
  image: my-registry/subsched-worker-rust@sha256:abcd...
  network: subsched-internal
  proxy_url: http://subsched-proxy:3128
  proxy_image: my-registry/subsched-proxy@sha256:...
  auth:
    claude: /path/to/claude-auth
    codex: /path/to/codex-auth

verification:
  commands:
    - cargo fmt --check
    - cargo test
```

### 5. Validate with `subsched doctor`
Run `subsched doctor` to confirm that:
- The container runtime and internal network are attested
- All executables listed in `verification.commands` exist inside the worker image

## Proxy policy and connectivity check

The reference policy permits HTTPS CONNECT only to `api.anthropic.com`, `chatgpt.com`,
and `auth0.openai.com`. `chatgpt.com` carries Codex requests authenticated by a ChatGPT
subscription; the metered OpenAI API endpoint is intentionally absent. The proxy tunnels TLS
without interception, which preserves the Codex client's TLS handshake and request headers.
It disables reverse-DNS matching for IP-literal requests and rejects provider names that resolve
to loopback, private, or link-local addresses before applying the domain allowlist.

The opt-in integration suite builds and exercises the real reference image with controlled DNS
answers. It covers an IP literal with an allowed reverse name, allowed names resolving to
prohibited addresses, and a permitted provider name resolving to a public test target:

```bash
SUBSCHED_DOCKER_PROXY_TEST=1 uv run pytest -q \
  tests/integration/test_reference_proxy_policy.py
```

After attaching the proxy to the internal and outbound networks as described in the main
README, test from the same worker image and network used by Scheduler. An unauthenticated
response may be `401` or another non-success status, but `000` means connectivity failed and
`403` reproduces the reported Cloudflare rejection; either result must block native dispatch
until the operator resolves it:

```bash
docker run --rm --network subsched-provider-internal \
  -e HTTPS_PROXY=http://subsched-provider-proxy:3128 \
  my-registry/subsched-worker-python@sha256:<worker-digest> \
  sh -c 'code="$(curl -sS -o /dev/null -w "%{http_code}" https://chatgpt.com/backend-api/codex)" && test "$code" != 000 && test "$code" != 403'
```
