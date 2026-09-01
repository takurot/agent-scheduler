from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from subsched.agents.process import redact_sensitive_command_audit
from subsched.gitenv import git_safe_env
from subsched.models import AgentResult
from subsched.storage import atomic_write_secure_bytes, secure_directory

MAX_CHECKPOINT_OUTPUT_BYTES = 65536


@dataclass(frozen=True, slots=True)
class MechanicalCheckpoint:
    issue_number: int
    git_status: str
    git_diff_stat: str
    changed_files: tuple[str, ...]
    head_commit: str
    test_results: str
    last_agent_output: str
    exit_code: int
    failure_classification: str
    timestamp: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> MechanicalCheckpoint:
        changed_raw = data.get("changed_files", ())
        changed_tuple = (
            tuple(str(f) for f in changed_raw) if isinstance(changed_raw, (list, tuple)) else ()
        )
        return cls(
            issue_number=int(str(data.get("issue_number", 0))),
            git_status=str(data.get("git_status", "")),
            git_diff_stat=str(data.get("git_diff_stat", "")),
            changed_files=changed_tuple,
            head_commit=str(data.get("head_commit", "")),
            test_results=str(data.get("test_results", "")),
            last_agent_output=str(data.get("last_agent_output", "")),
            exit_code=int(str(data.get("exit_code", 0))),
            failure_classification=str(data.get("failure_classification", "")),
            timestamp=str(data.get("timestamp", "")),
        )


def capture_mechanical_checkpoint(
    worktree_dir: Path,
    issue_number: int,
    agent_result: AgentResult,
    exit_code: int = 0,
    test_results: str = "",
) -> MechanicalCheckpoint:
    """Capture mechanical state of the worktree after an agent invocation or termination."""
    status = _run_git(worktree_dir, ["status", "--short"])
    diff_stat = _run_git(worktree_dir, ["diff", "--stat"])
    changed_raw = _run_git(worktree_dir, ["status", "--porcelain"])
    changed_files = tuple(
        line[2:].strip() for line in changed_raw.splitlines() if len(line) > 2
    )
    head_commit = _run_git(worktree_dir, ["rev-parse", "HEAD"])

    output = redact_sensitive_command_audit((agent_result.output,))[0]
    if len(output.encode("utf-8")) > MAX_CHECKPOINT_OUTPUT_BYTES:
        output = output[:MAX_CHECKPOINT_OUTPUT_BYTES] + "... [truncated]"

    now_ts = datetime.now(UTC).isoformat()
    return MechanicalCheckpoint(
        issue_number=issue_number,
        git_status=status,
        git_diff_stat=diff_stat,
        changed_files=changed_files,
        head_commit=head_commit,
        test_results=test_results,
        last_agent_output=output,
        exit_code=exit_code,
        failure_classification=agent_result.kind.value,
        timestamp=now_ts,
    )


def save_checkpoint(worktree_dir: Path, checkpoint: MechanicalCheckpoint) -> Path:
    """Persist mechanical checkpoint under .ai/checkpoints/<issue>.json.

    #143: checkpoints can contain changed-file lists, test output, and agent output
    summaries -- runtime metadata other users on the same machine should not be able to
    read. The directory and file are hardened to 0700/0600 (matching JsonStateStore's
    backup handling) via atomic_write_secure_bytes/secure_directory, which also refuse a
    symlinked target and repair permissions on any pre-existing checkpoint left behind
    by an older subsched version, without deleting it.
    """
    checkpoints_dir = worktree_dir / ".ai" / "checkpoints"
    secure_directory(checkpoints_dir)
    target = checkpoints_dir / f"{checkpoint.issue_number}.json"
    data = json.dumps(checkpoint.to_dict(), indent=2)
    atomic_write_secure_bytes(target, data.encode("utf-8"))
    return target


def load_checkpoint(worktree_dir: Path, issue_number: int) -> MechanicalCheckpoint | None:
    """Load persisted checkpoint for an issue."""
    target = worktree_dir / ".ai" / "checkpoints" / f"{issue_number}.json"
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        return MechanicalCheckpoint.from_dict(data)
    except Exception:
        return None


def _run_git(cwd: Path, args: list[str]) -> str:
    try:
        res = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=git_safe_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return res.stdout.strip()
    except Exception:
        return ""
