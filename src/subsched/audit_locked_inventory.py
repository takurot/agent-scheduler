"""Derive a Python audit inventory without executing or mounting task code.

The image and container runtime must be attested by the operator; the image must
contain the offline wheel bundle prepared by the offline verification procedure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.agents.isolation import cleanup_native_container, native_container_name
from subsched.agents.process import run_process_group
from subsched.audit_inventory import canonical_inventory
from subsched.audit_provenance import _unique_object

_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[0-9a-f]{64}\Z")
_MAX_INPUT = 8 * 1024 * 1024


class AuditIsolationError(ValueError):
    """The isolated inventory process or container cleanup could not be confirmed."""


_SCRIPT = r"""set -eu
mkdir /tmp/audit-project
python3 -S -c '
import base64, json, pathlib, sys
d = json.load(sys.stdin)
p = pathlib.Path("/tmp/audit-project")
for key, name in (("lock", "uv.lock"), ("project", "pyproject.toml")):
    (p / name).write_bytes(base64.b64decode(d[key], validate=True))
'
(cd /tmp/audit-project && uv export --frozen --offline --no-config --no-emit-project \
    --no-header --no-annotate > /tmp/exported-requirements)
uv venv /tmp/venv --python /usr/local/bin/python3 --offline --no-config >/dev/null 2>&1
uv pip sync --python /tmp/venv/bin/python --offline --no-index \
    --find-links /opt/subsched-offline/wheels --require-hashes --no-cache --no-config --no-build \
    /opt/subsched-offline/requirements.txt >/dev/null 2>&1
uv pip install --python /tmp/venv/bin/python --offline --no-index \
    --find-links /opt/subsched-offline/wheels --require-hashes --no-cache --no-config --no-build \
    --no-deps -r /opt/subsched-offline/build-requirements.txt >/dev/null 2>&1
/tmp/venv/bin/python -S - <<'PY'
import hashlib, importlib.metadata, json, subprocess, sys, sysconfig
from pathlib import Path
base = Path("/opt/subsched-offline")
site = Path("/tmp/venv/lib") / ("python%d.%d" % sys.version_info[:2]) / "site-packages"
if not site.is_dir():
    raise SystemExit("missing package metadata directory")
def digest(name):
    return hashlib.sha256((base / name).read_bytes()).hexdigest()
print(json.dumps({
    "manifest": json.loads((base / "manifest.json").read_text()),
    "identity": {"python": sys.version.split()[0], "platform": sysconfig.get_platform(),
                 "uv": subprocess.check_output(["uv", "--version"], text=True).strip()},
    "requirements_sha256": digest("requirements.txt"),
    "export_sha256": hashlib.sha256(Path("/tmp/exported-requirements").read_bytes()).hexdigest(),
    "build_requirements_sha256": digest("build-requirements.txt"),
    "packages": [[d.metadata["Name"], d.version]
                 for d in importlib.metadata.distributions(path=[str(site)])],
}))
PY
"""


def _read(path: Path) -> bytes:
    if any(item.is_symlink() for item in (path, *path.parents)) or not path.is_file():
        raise ValueError("unsafe audit input")
    if path.stat().st_size > _MAX_INPUT:
        raise ValueError("audit input too large")
    return path.read_bytes()


def derive_locked_inventory(
    project_dir: Path,
    image: str,
    runtime: Path,
    *,
    runner: Callable[[ProcessExecutionRequest], ProcessExecutionResult] = run_process_group,
    cleanup: Callable[..., str | None] = cleanup_native_container,
) -> list[tuple[str, str]]:
    """Resolve wheels from an attested image without running the target project."""
    if not project_dir.is_absolute() or not _IMAGE.fullmatch(image) or not runtime.is_absolute():
        raise ValueError("invalid audit isolation parameters")
    lock = _read(project_dir / "uv.lock")
    project_bytes = _read(project_dir / "pyproject.toml")
    build = _read(project_dir / "scripts" / "offline-build-requirements.txt")
    try:
        project = tomllib.loads(project_bytes.decode("utf-8"))["project"]
        root = (project["name"], project["version"])
        canonical_inventory([root])
    except (KeyError, TypeError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError("project metadata must have a static name and version") from error
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "HOME",
            "PATH",
            "DOCKER_HOST",
            "DOCKER_CERT_PATH",
            "DOCKER_TLS_VERIFY",
            "CONTAINER_HOST",
        }
    }
    name = native_container_name("audit-inventory")
    argv = (
        str(runtime),
        "run",
        "--rm",
        "--interactive",
        "--pull",
        "never",
        "--name",
        name,
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--pids-limit",
        "128",
        "--memory",
        "1g",
        "--cpus",
        "1",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=1g,mode=1777",
        "--env",
        "HOME=/tmp",
        "--env",
        "UV_CACHE_DIR=/tmp/cache",
        "--entrypoint",
        "/bin/sh",
        image,
        "-ceu",
        _SCRIPT,
    )
    request = ProcessExecutionRequest(
        argv=argv,
        cwd=Path(tempfile.gettempdir()).resolve(),
        env=env,
        stdin_payload=json.dumps(
            {
                "lock": base64.b64encode(lock).decode("ascii"),
                "project": base64.b64encode(project_bytes).decode("ascii"),
            }
        ).encode(),
        timeout_seconds=120,
        output_limit_bytes=2 * 1024 * 1024,
    )
    cleanup_error: str | None = None
    try:
        try:
            result = runner(request)
        finally:
            try:
                cleanup_error = cleanup(runtime, name, env=env)
            except Exception as error:
                raise AuditIsolationError("audit inventory cleanup unconfirmed") from error
    except Exception as error:
        if cleanup_error is not None:
            raise AuditIsolationError("audit inventory cleanup unconfirmed") from error
        raise
    if cleanup_error is not None or not result.cleanup_succeeded:
        raise AuditIsolationError("audit inventory cleanup unconfirmed")
    if (
        result.exit_code != 0
        or result.timed_out
        or result.output_limit_exceeded
        or result.command_not_found
    ):
        raise ValueError("audit inventory container failed")
    try:
        data: dict[str, Any] = json.loads(result.stdout, object_pairs_hook=_unique_object)
        manifest = data["manifest"]
        identity = manifest["identity"]
        if (
            type(manifest["version"]) is not int
            or manifest["version"] != 1
            or not isinstance(identity, dict)
            or set(identity) != {"python", "platform", "uv"}
            or not all(isinstance(value, str) and value.strip() for value in identity.values())
            or not isinstance(manifest["requirements_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest["requirements_sha256"]) is None
            or manifest["lock_sha256"] != hashlib.sha256(lock).hexdigest()
            or manifest["project_sha256"] != hashlib.sha256(project_bytes).hexdigest()
            or manifest["build_requirements_sha256"] != hashlib.sha256(build).hexdigest()
            or manifest["build_requirements_sha256"] != data["build_requirements_sha256"]
            or manifest["requirements_sha256"] != data["requirements_sha256"]
            or manifest["requirements_sha256"] != data["export_sha256"]
            or manifest["identity"] != data["identity"]
        ):
            raise ValueError("offline image is stale or inconsistent")
        packages = data["packages"]
        if not isinstance(packages, list) or any(
            not isinstance(item, list) or len(item) != 2 for item in packages
        ):
            raise ValueError("invalid package inventory")
        inventory = [root, *(tuple(item) for item in packages)]
        return [
            (item["name"], item["version"]) for item in json.loads(canonical_inventory(inventory))
        ]
    except (KeyError, TypeError, UnicodeError, RecursionError, json.JSONDecodeError) as error:
        raise ValueError("invalid audit inventory response") from error
