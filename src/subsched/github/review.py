from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum

from subsched.agents.process import redact_sensitive_command_audit

MAX_COMMENT_CHARS = 16000


class PostCommentResultKind(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class PostCommentResult:
    kind: PostCommentResultKind
    output: str = ""


def _redact(text: str) -> str:
    return "\n".join(redact_sensitive_command_audit(tuple(text.splitlines())))


def post_pr_comment(
    pr_number: int,
    body: str,
    repo: str | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> PostCommentResult:
    """Post a review summary comment to a PR via `gh pr comment`.

    Best-effort, Scheduler-owned side effect (the reviewer Agent never has GitHub write
    access itself -- see build_review_prompt). Failures are surfaced to the caller for
    logging but must never be treated as ambiguous state that blocks the review verdict
    itself: the review report file is the source of truth for the verdict, this comment
    is purely a human-facing notification.
    """
    truncated = body
    if len(truncated) > MAX_COMMENT_CHARS:
        truncated = truncated[:MAX_COMMENT_CHARS] + "... [truncated]"
    argv = ["gh", "pr", "comment", str(pr_number), "--body", truncated]
    if repo:
        argv.extend(["--repo", repo])
    try:
        res = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return PostCommentResult(
            kind=PostCommentResultKind.FAILURE,
            output=f"could not post PR comment (invocation failed: {err})",
        )
    if res.returncode != 0:
        return PostCommentResult(
            kind=PostCommentResultKind.FAILURE,
            output=_redact(
                f"gh pr comment exited {res.returncode}: {res.stderr.strip()}"
            ),
        )
    return PostCommentResult(kind=PostCommentResultKind.SUCCESS, output=res.stdout.strip())
