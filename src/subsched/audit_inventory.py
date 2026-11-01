"""Validate a data-only dependency inventory and pip-audit's JSON response.

These helpers do not resolve dependencies, invoke a producer, or authenticate an
audit result. A caller must independently establish the inventory's completeness
and the audit tool's trust before using the resulting classification.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import tempfile
import zlib
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from subsched.agents.base import ProcessExecutionRequest, ProcessExecutionResult
from subsched.agents.process import run_process_group

_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z")
_VERSION = re.compile(r"[0-9][A-Za-z0-9.!+_-]*\Z")
_MAX_PACKAGES = 10000
_MAX_REPORT = 2 * 1024 * 1024
_MAX_RESPONSE = 1024 * 1024
_AUDIT_BOOTSTRAP = (
    "import os,pathlib,runpy,sys; "
    "os.environ['PIPAPI_PYTHON_LOCATION']=str(pathlib.Path.cwd()/'pip-python'); "
    "sys.path.insert(0,str(pathlib.Path(sys.executable).parent.parent/'lib'/"
    "('python%d.%d'%sys.version_info[:2])/'site-packages')); "
    "runpy.run_module('pip_audit',run_name='__main__',alter_sys=True)"
)
_PIP_BOOTSTRAP = (
    "import pathlib,runpy,sys; "
    "sys.path.insert(0,str(pathlib.Path(sys.executable).parent.parent/'lib'/"
    "('python%d.%d'%sys.version_info[:2])/'site-packages')); "
    "runpy.run_module('pip',run_name='__main__',alter_sys=True)"
)


def _normalized(items: Sequence[tuple[str, str]]) -> list[dict[str, str]]:
    if not items or len(items) > _MAX_PACKAGES:
        raise ValueError("inventory must contain a bounded package set")
    packages: dict[str, str] = {}
    for name, version in items:
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or len(name) > 200
            or len(version) > 200
            or _NAME.fullmatch(name) is None
            or _VERSION.fullmatch(version) is None
        ):
            raise ValueError("invalid package name or version")
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in packages:
            raise ValueError("duplicate package name")
        packages[normalized] = version
    return [{"name": name, "version": packages[name]} for name in sorted(packages)]


def canonical_inventory(items: Sequence[tuple[str, str]]) -> bytes:
    return json.dumps(_normalized(items), separators=(",", ":"), ensure_ascii=True).encode("ascii")


def expected_cache_paths(items: Sequence[tuple[str, str]]) -> set[str]:
    paths = set()
    for package in _normalized(items):
        url = f"https://pypi.org/pypi/{package['name']}/{package['version']}/json"
        digest = hashlib.sha224(url.encode("ascii")).hexdigest()
        paths.add("/".join([*digest[:5], digest]))
    return paths


def _cache_complete(cache: Path, items: Sequence[tuple[str, str]]) -> bool:
    packages = _normalized(items)
    expected = {
        next(iter(expected_cache_paths([(package["name"], package["version"])]))): package
        for package in packages
    }
    actual: set[str] = set()
    total = 0
    for path in cache.rglob("*"):
        if path.is_symlink() or not (path.is_dir() or path.is_file()):
            return False
        if path.is_file():
            size = path.stat().st_size
            if not 0 < size <= 1024 * 1024:
                return False
            total += size
            if total > 16 * 1024 * 1024:
                return False
            relative = path.relative_to(cache).as_posix()
            if relative not in expected or not _valid_cached_response(path, expected[relative]):
                return False
            actual.add(relative)
    return actual == set(expected)


def _valid_cached_response(path: Path, package: dict[str, str]) -> bool:
    try:
        import msgpack  # type: ignore[import-untyped]
    except ImportError:
        return False
    try:
        data = path.read_bytes()
        if not data.startswith(b"cc=4,"):
            return False
        response = msgpack.unpackb(data[5:], raw=False)["response"]
        headers = {key.lower(): value for key, value in response["headers"].items()}
        body = response["body"]
        if response["status"] != 200 or not isinstance(body, bytes):
            return False
        encoding = headers.get("content-encoding", "identity").lower()
        if encoding == "gzip":
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = decoder.decompress(body, _MAX_RESPONSE + 1)
            if not decoder.eof or decoder.unconsumed_tail or decoder.unused_data:
                return False
        elif encoding != "identity":
            return False
        if len(body) > _MAX_RESPONSE:
            return False
        payload = json.loads(body, object_pairs_hook=_unique_object)
        info = payload["info"]
        return (
            _normalized([(info["name"], info["version"])]) == [package]
            and payload["vulnerabilities"] == []
            and isinstance(payload["vulnerabilities"], list)
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError, UnicodeError,
            RecursionError, zlib.error, msgpack.UnpackException):
        return False


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON field")
        output[key] = value
    return output


def inspect_audit_result(
    items: Sequence[tuple[str, str]], result: subprocess.CompletedProcess[bytes]
) -> str:
    """Distinguish clean/advisory/unavailable without trusting an exit code alone."""
    try:
        expected = _normalized(items)
        if (
            result.returncode not in (0, 1)
            or not isinstance(result.stdout, bytes)
            or len(result.stdout) > _MAX_REPORT
        ):
            return "unavailable"
        report = json.loads(result.stdout, object_pairs_hook=_unique_object)
        if not isinstance(report, dict) or set(report) != {"dependencies", "fixes"}:
            return "unavailable"
        dependencies = report["dependencies"]
        if not isinstance(dependencies, list) or report["fixes"] != []:
            return "unavailable"
        actual: list[tuple[str, str]] = []
        advisory = False
        for item in dependencies:
            if not isinstance(item, dict) or not isinstance(item.get("vulns"), list):
                return "unavailable"
            for vulnerability in item["vulns"]:
                if (
                    not isinstance(vulnerability, dict)
                    or not isinstance(vulnerability.get("id"), str)
                    or not vulnerability["id"].strip()
                ):
                    return "unavailable"
                advisory = True
            actual.append((item["name"], item["version"]))
        if _normalized(actual) != expected or (result.returncode == 0) == advisory:
            return "unavailable"
        return "advisory" if advisory else "clean"
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        return "unavailable"


def run_data_audit(
    items: Sequence[tuple[str, str]],
    tool: Path,
    expected_version: str,
    *,
    cache_dir: Path | None = None,
    runner: Callable[[ProcessExecutionRequest], ProcessExecutionResult] = run_process_group,
) -> str:
    """Run an operator-trusted audit tool on pinned data without installing target code."""
    try:
        packages = _normalized(items)
        if (
            not tool.is_absolute()
            or tool.is_symlink()
            or not tool.is_file()
            or re.fullmatch(r"pip-audit [0-9][A-Za-z0-9.!+_-]*", expected_version) is None
        ):
            return "unavailable"
        with tempfile.TemporaryDirectory(prefix="subsched-data-audit-") as directory:
            cwd = Path(directory).resolve()
            env = {"HOME": str(cwd), "PATH": "/usr/bin:/bin"}
            shim = cwd / "pip-python"
            shim.write_text(
                "#!/bin/sh\n"
                '[ "$#" -eq 3 ] && [ "$1" = "-m" ] && [ "$2" = "pip" ] '
                '&& [ "$3" = "--version" ] || exit 126\n'
                f"exec {shlex.quote(str(tool.parent / 'python'))} -I -S -c "
                f"{shlex.quote(_PIP_BOOTSTRAP)} --version\n",
                encoding="utf-8",
            )
            shim.chmod(0o700)
            if cache_dir is not None and (
                not cache_dir.is_absolute()
                or cache_dir.is_symlink()
                or not cache_dir.is_dir()
                or any(parent.is_symlink() for parent in cache_dir.parents)
                or any(cache_dir.iterdir())
            ):
                return "unavailable"
            effective_cache = cache_dir if cache_dir is not None else cwd / "cache"
            version = runner(
                ProcessExecutionRequest(
                    argv=(str(tool.parent / "python"), "-I", "-S", "-c", _AUDIT_BOOTSTRAP,
                          "--version"),
                    cwd=cwd,
                    env=env,
                    timeout_seconds=10,
                    output_limit_bytes=4096,
                )
            )
            if (
                version.exit_code != 0
                or version.stdout.strip() != expected_version
                or version.timed_out
                or version.output_limit_exceeded
                or version.command_not_found
                or not version.cleanup_succeeded
            ):
                return "unavailable"
            requirements = cwd / "requirements.txt"
            requirements.write_text(
                "".join(f"{package['name']}=={package['version']}\n" for package in packages),
                encoding="ascii",
            )
            result = runner(
                ProcessExecutionRequest(
                    argv=(
                        str(tool.parent / "python"),
                        "-I",
                        "-S",
                        "-c",
                        _AUDIT_BOOTSTRAP,
                        "--disable-pip",
                        "--no-deps",
                        "--format",
                        "json",
                        "--timeout",
                        "10",
                        "--cache-dir",
                        str(effective_cache),
                        "-r",
                        str(requirements),
                    ),
                    cwd=cwd,
                    env=env,
                    timeout_seconds=120,
                    output_limit_bytes=_MAX_REPORT,
                )
            )
            if (
                result.timed_out
                or result.output_limit_exceeded
                or result.command_not_found
                or not result.cleanup_succeeded
            ):
                return "unavailable"
            report = subprocess.CompletedProcess(
                args=[],
                returncode=result.exit_code,
                stdout=result.stdout.encode("utf-8"),
                stderr=b"",
            )
            outcome = inspect_audit_result(items, report)
            if (
                outcome == "clean"
                and cache_dir is not None
                and not _cache_complete(cache_dir, items)
            ):
                return "unavailable"
            return outcome
    except (OSError, ValueError, TypeError, UnicodeError):
        return "unavailable"


def produce_audit_record(
    items: Sequence[tuple[str, str]],
    target: dict[str, str],
    tool: Path,
    observed_at: datetime,
    *,
    cache_dir: Path | None = None,
    runner: Callable[[ProcessExecutionRequest], ProcessExecutionResult] = run_process_group,
) -> dict[str, Any]:
    """Bind an audit result to its independently checked target and input inventory."""
    required = {
        "repository",
        "commit",
        "lock_sha256",
        "packages_sha256",
        "tool",
        "policy",
        "workflow_sha256",
    }
    if (
        target.keys() != required
        or not all(isinstance(value, str) and value.strip() for value in target.values())
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", target["repository"]) is None
        or re.fullmatch(r"[0-9a-f]{40}", target["commit"]) is None
        or any(
            re.fullmatch(r"[0-9a-f]{64}", target[key]) is None
            for key in ("lock_sha256", "packages_sha256", "workflow_sha256")
        )
        or hashlib.sha256(canonical_inventory(items)).hexdigest() != target["packages_sha256"]
        or observed_at.tzinfo is None
        or observed_at.utcoffset() is None
    ):
        raise ValueError("invalid audit target or inventory")
    outcome = run_data_audit(items, tool, target["tool"], cache_dir=cache_dir, runner=runner)
    return {
        "schema": 1,
        "target": dict(target),
        "observed_at": observed_at.isoformat(),
        "outcome": outcome,
    }
