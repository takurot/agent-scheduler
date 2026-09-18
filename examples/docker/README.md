# Worker Container Images for subsched Isolation

When running `subsched` with container isolation (`isolation.backend: container`), worker containers execute with:
- A read-only root filesystem (`--read-only`)
- An internal Docker network (`Internal: true`) without direct internet access
- Strict proxy allowlisting permitting only model provider API endpoints (Anthropic, OpenAI)

Because the container cannot access package repositories or toolchain installers at runtime, **all compilers, language runtimes, linters, and test runners needed by your repository must be pre-baked into the worker container image** (#364).

## Reference Dockerfiles

This directory provides reference Dockerfiles for common project languages:

- [`Dockerfile.worker-rust`](Dockerfile.worker-rust): Debian bookworm base with Node.js, Claude Code/Codex CLIs, `procps`, and full Rust toolchain (`cargo`, `rustc`, `rustfmt`, `clippy`).
- [`Dockerfile.worker-python`](Dockerfile.worker-python): Debian bookworm base with Node.js, Claude Code/Codex CLIs, `procps`, Python 3, and `uv`.

## Building and Pining Worker Images

### 1. Build the image
```bash
# For a Rust project:
docker build -t my-registry/subsched-worker-rust:v1 -f examples/docker/Dockerfile.worker-rust .

# For a Python project:
docker build -t my-registry/subsched-worker-python:v1 -f examples/docker/Dockerfile.worker-python .
```

### 2. Push to your container registry (or keep local)
```bash
docker push my-registry/subsched-worker-rust:v1
```

### 3. Obtain the immutable RepoDigest
Container isolation requires pinning images by immutable sha256 digest (`RepoDigests`):
```bash
docker inspect my-registry/subsched-worker-rust:v1 --format '{{index .RepoDigests 0}}'
# Example output: my-registry/subsched-worker-rust@sha256:abcd...
```

### 4. Configure `subsched.yaml`
```yaml
isolation:
  backend: container
  runtime: docker
  image: my-registry/subsched-worker-rust@sha256:abcd...
  network: subsched-internal
  proxy_url: http://subsched-proxy:3128
  proxy_image: ghcr.io/example/proxy@sha256:...
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
