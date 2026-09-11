from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from subsched.agents.process import redact_sensitive_command_audit
from subsched.gitenv import git_safe_env
from subsched.handoff import parse_semantic_handoff
from subsched.models import Task

# Shared between the PR body sanitizer (_strip_close_keywords) and the commit-message
# gate (find_close_keyword_commits) so both enforce the exact same auto-close policy
# (regression test for #140: previously only PR body text was checked).
CLOSE_KEYWORD_RE = re.compile(r"\b(fix(e[sd])?|close[sd]?|resolve[sd]?)\s*#", re.IGNORECASE)
MAX_PR_SECTION_CHARS = 16000


def _strip_close_keywords(text: str) -> str:
    """Replace GitHub auto-closing keywords with safe issue reference."""
    return CLOSE_KEYWORD_RE.sub("issue #", text)


@dataclass(frozen=True, slots=True)
class PullRequestInfo:
    number: int
    url: str
    title: str
    body: str


class PullRequestResultKind(StrEnum):
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"


@dataclass(frozen=True, slots=True)
class PullRequestResult:
    kind: PullRequestResultKind
    info: PullRequestInfo | None
    output: str = ""


def _redact(text: str) -> str:
    return "\n".join(redact_sensitive_command_audit(tuple(text.splitlines())))


def _sanitize_pr_section(text: str) -> str:
    sanitized = _strip_close_keywords(_redact(text))
    if len(sanitized) > MAX_PR_SECTION_CHARS:
        return sanitized[:MAX_PR_SECTION_CHARS] + "... [truncated]"
    return sanitized


@dataclass(frozen=True, slots=True)
class CloseKeywordViolation:
    commit: str
    keyword_context: str


_LOG_RECORD_SEP = "\x1e"
_LOG_FIELD_SEP = "\x1f"


def find_close_keyword_commits(
    worktree_dir: Path,
    base_branch: str,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> tuple[CloseKeywordViolation, ...] | None:
    """Scan commits reachable from HEAD but not from base_branch for GitHub auto-close
    keywords (Fixes/Closes/Resolves #N) anywhere in the full commit message.

    Regression coverage for #140: a worker's local commit message was never checked
    before push (only the generated PR body was sanitized), so a commit whose message
    happened to end with e.g. "Closes #130" could trigger GitHub's merge-time
    auto-close, bypassing the "issues stay open until manual review" invariant.

    Returns an empty tuple when no commit is reachable from HEAD but not base_branch,
    or when none contain a close keyword. Returns None (fail closed) if the commits
    could not be inspected at all (e.g. git failure), so the caller must not treat
    "could not check" as "clean". Never rewrites history -- read-only `git log`.
    """
    argv = [
        "git",
        "-C",
        str(worktree_dir),
        "log",
        f"{base_branch}..HEAD",
        f"--format=%H{_LOG_FIELD_SEP}%B{_LOG_RECORD_SEP}",
    ]
    try:
        res = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            env=git_safe_env(env),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if res.returncode != 0:
        return None

    violations: list[CloseKeywordViolation] = []
    for record in res.stdout.split(_LOG_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        commit_sha, _, message = record.partition(_LOG_FIELD_SEP)
        if CLOSE_KEYWORD_RE.search(message):
            violations.append(
                CloseKeywordViolation(
                    commit=commit_sha[:12], keyword_context=_redact(message.strip())
                )
            )
    return tuple(violations)


# Default handoff content stamped by bootstrap_task_files (contract.py) before any
# real progress has been recorded -- treated as "unpopulated" so a freshly-bootstrapped
# worktree falls back to commit log / task.title instead of echoing boilerplate (#249).
_PLACEHOLDER_HANDOFF_VALUES = frozenset({"- task bootstrapped", "- none yet"})


def _extract_commit_log_summary(
    worktree_dir: Path,
    base_branch: str,
    env: dict[str, str] | None,
    timeout_seconds: float,
) -> str:
    """Summarize commits reachable from HEAD but not base_branch as a bullet list of
    subject lines (with any body lines indented underneath). Read-only `git log`;
    returns "" (not None) on any failure so the caller can fall back safely.
    """
    argv = [
        "git",
        "-C",
        str(worktree_dir),
        "log",
        f"{base_branch}..HEAD",
        f"--format=%s{_LOG_FIELD_SEP}%b{_LOG_RECORD_SEP}",
    ]
    try:
        res = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_seconds,
            env=git_safe_env(env),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if res.returncode != 0:
        return ""

    entries: list[str] = []
    for record in res.stdout.split(_LOG_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        subject, _, body = record.partition(_LOG_FIELD_SEP)
        subject = subject.strip()
        if not subject:
            continue
        entry = f"- {subject}"
        body_lines = "\n".join(f"  {line}" for line in body.strip().splitlines() if line.strip())
        if body_lines:
            entry = f"{entry}\n{body_lines}"
        entries.append(entry)
    if not entries:
        return ""
    return "### Commits\n\n" + "\n".join(entries)


def extract_pr_summary(
    worktree_dir: Path,
    task: Task,
    base_branch: str,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> str:
    """Build a rich PR summary from the worktree's semantic handoff and commit log
    (#249), so reviewers see what changed without inspecting the full diff.

    Preference order: populated handoff `## Completed` (plus `## Decisions` if
    present) -> commit subjects/bodies from `git log base_branch..HEAD` -> task.title.
    The caller must still pass the result through `_sanitize_pr_section` (redaction,
    auto-close keyword stripping, length cap) -- this function only assembles content.
    """
    handoff_file = worktree_dir / ".ai" / "handoffs" / f"{task.issue_number}.md"
    if handoff_file.is_file():
        try:
            content = handoff_file.read_text(encoding="utf-8")
        except OSError:
            content = ""
        parsed = parse_semantic_handoff(content) if content else None
        if parsed is not None:
            completed = parsed.completed.strip()
            if completed and completed.lower() not in _PLACEHOLDER_HANDOFF_VALUES:
                sections = [f"### Completed Changes\n\n{completed}"]
                decisions = parsed.decisions.strip()
                if decisions and decisions.lower() not in _PLACEHOLDER_HANDOFF_VALUES:
                    sections.append(f"### Key Decisions\n\n{decisions}")
                return "\n\n".join(sections)

    commit_summary = _extract_commit_log_summary(worktree_dir, base_branch, env, timeout_seconds)
    if commit_summary:
        return commit_summary

    return task.title


def build_pr_body(
    issue_number: int,
    summary: str = "",
    verification_results: str = "",
    *,
    close_issue: bool = False,
) -> str:
    """Build safe PR body according to SPEC (avoids auto-closing unless close_issue=True)."""
    v_raw = verification_results if verification_results.strip() else "- All checks passed: PASS"
    s_raw = (
        summary.strip()
        if summary.strip()
        else f"Implementation completed for issue #{issue_number}."
    )
    v_section = _sanitize_pr_section(v_raw)
    s_section = _sanitize_pr_section(s_raw)
    footer = (
        f"Closes #{issue_number}."
        if close_issue
        else "Issue is intentionally left open until review."
    )
    return (
        f"Implements work for #{issue_number}.\n\n"
        f"## Summary\n\n{s_section}\n\n"
        f"## Verification\n\n{v_section}\n\n"
        f"Generated by Subscription Scheduler.\n\n"
        f"{footer}\n"
    )


class ExistingPrCheckKind(StrEnum):
    NONE = "NONE"
    CONFIRMED = "CONFIRMED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class ExistingPrCheckResult:
    kind: ExistingPrCheckKind
    info: PullRequestInfo | None = None
    reason: str = ""


def lookup_existing_pr(
    branch_name: str,
    issue_number: int | None = None,
    base: str = "main",
    repo: str | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> ExistingPrCheckResult:
    """Lookup if an open PR already exists for this branch to ensure idempotency.

    Untrusted-schema handling: PR data from GitHub is validated before reuse.
    Only an open PR whose headRefName matches branch_name, whose baseRefName matches
    the expected base branch, and whose body starts with the Scheduler-generated
    "Implements work for #N." prefix (when issue_number is provided) is CONFIRMED.
    Multiple candidates, branch/base mismatch, missing/wrong prefix, or gh failures
    are AMBIGUOUS and fail closed to avoid adopting an unrelated or manual PR (#188).
    """
    argv = [
        "gh",
        "pr",
        "list",
        "--head",
        branch_name,
        "--state",
        "open",
        "--json",
        "number,url,title,body,headRefName,baseRefName",
        "--limit",
        "10",
    ]
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
        return ExistingPrCheckResult(
            kind=ExistingPrCheckKind.AMBIGUOUS,
            reason=f"could not query existing PRs for {branch_name} (invocation failed: {err})",
        )
    if res.returncode != 0:
        err_msg = res.stderr.strip()
        return ExistingPrCheckResult(
            kind=ExistingPrCheckKind.AMBIGUOUS,
            reason=_redact(
                f"could not query existing PRs for {branch_name} "
                f"(gh exited {res.returncode}: {err_msg})"
            ),
        )
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return ExistingPrCheckResult(
            kind=ExistingPrCheckKind.AMBIGUOUS,
            reason=f"could not query existing PRs for {branch_name} (unparseable gh output)",
        )
    if not isinstance(data, list):
        return ExistingPrCheckResult(
            kind=ExistingPrCheckKind.AMBIGUOUS,
            reason=f"could not query existing PRs for {branch_name} (invalid gh output structure)",
        )
    if not data:
        return ExistingPrCheckResult(kind=ExistingPrCheckKind.NONE)

    if len(data) > 1:
        numbers = [str(item.get("number", "?")) for item in data if isinstance(item, dict)]
        return ExistingPrCheckResult(
            kind=ExistingPrCheckKind.AMBIGUOUS,
            reason=(
                f"multiple open PRs found for branch {branch_name} ({', '.join(numbers)}); "
                "review manually before reusing"
            ),
        )

    item = data[0]
    if not isinstance(item, dict):
        return ExistingPrCheckResult(
            kind=ExistingPrCheckKind.AMBIGUOUS,
            reason=f"could not verify existing PR for {branch_name} (malformed item)",
        )

    try:
        number = int(item["number"])
        url = str(item["url"])
        title = str(item["title"])
        body = str(item.get("body", ""))
        head_ref = str(item.get("headRefName", ""))
        base_ref = str(item.get("baseRefName", ""))
    except (KeyError, TypeError, ValueError) as err:
        return ExistingPrCheckResult(
            kind=ExistingPrCheckKind.AMBIGUOUS,
            reason=f"could not verify existing PR for {branch_name} (malformed payload: {err})",
        )

    if head_ref != branch_name:
        return ExistingPrCheckResult(
            kind=ExistingPrCheckKind.AMBIGUOUS,
            reason=(
                f"existing PR #{number} headRefName '{head_ref}' does not match "
                f"expected '{branch_name}'"
            ),
        )

    if base_ref != base:
        return ExistingPrCheckResult(
            kind=ExistingPrCheckKind.AMBIGUOUS,
            reason=(
                f"existing PR #{number} targets base branch '{base_ref}', "
                f"expected '{base}'"
            ),
        )

    if issue_number is not None:
        expected_prefix = f"Implements work for #{issue_number}."
        if not body.startswith(expected_prefix):
            return ExistingPrCheckResult(
                kind=ExistingPrCheckKind.AMBIGUOUS,
                reason=(
                    f"existing PR #{number} body does not start with expected prefix "
                    f"'{expected_prefix}'"
                ),
            )

    info = PullRequestInfo(number=number, url=url, title=title, body=body)
    return ExistingPrCheckResult(kind=ExistingPrCheckKind.CONFIRMED, info=info)


class MergedPrCheckKind(StrEnum):
    NONE = "NONE"
    CONFIRMED = "CONFIRMED"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True, slots=True)
class MergedPrCheckResult:
    kind: MergedPrCheckKind
    pr_number: int | None = None
    reason: str = ""


def check_merged_pr_for_issue(
    repo: str,
    issue_number: int,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> MergedPrCheckResult:
    """Look for a merged PR that already implements this issue (#146: without this,
    discovery re-adds a merged-but-still-open issue as READY and duplicates work).

    Untrusted-schema handling: PR body/branch-name content from GitHub is never trusted
    blindly. Only a merged PR whose body starts with the exact Scheduler-generated
    "Implements work for #N." prefix (see build_pr_body) *and* whose branch matches the
    Scheduler's own naming convention (subsched/issue-N) is treated as CONFIRMED -- a
    strong, structural signal that the Scheduler itself created and merged this PR.
    Anything weaker (a merged PR that merely mentions the issue number, multiple
    candidates, or a gh failure) is AMBIGUOUS and must not be silently treated as
    confirmed-safe; callers fail closed on AMBIGUOUS by excluding the issue from READY
    and escalating it to NEEDS_HUMAN for manual review, matching the SPEC principle that
    the Scheduler never auto-closes or auto-completes ambiguous GitHub state.
    """
    argv = [
        "gh",
        "pr",
        "list",
        "--repo",
        repo,
        "--search",
        f"#{issue_number} in:body",
        "--state",
        "merged",
        "--json",
        "number,headRefName,body",
        "--limit",
        "10",
    ]
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
    except (OSError, subprocess.TimeoutExpired):
        return MergedPrCheckResult(
            kind=MergedPrCheckKind.AMBIGUOUS,
            reason=(
                f"could not verify whether issue #{issue_number} already has a merged "
                "PR (gh invocation failed); review manually before treating it as READY"
            ),
        )
    if res.returncode != 0:
        return MergedPrCheckResult(
            kind=MergedPrCheckKind.AMBIGUOUS,
            reason=(
                f"could not verify whether issue #{issue_number} already has a merged "
                f"PR (gh exited {res.returncode}); review manually before treating it as READY"
            ),
        )
    try:
        data = json.loads(res.stdout)
    except json.JSONDecodeError:
        return MergedPrCheckResult(
            kind=MergedPrCheckKind.AMBIGUOUS,
            reason=(
                f"could not verify whether issue #{issue_number} already has a merged "
                "PR (unparseable gh output); review manually before treating it as READY"
            ),
        )
    if not isinstance(data, list) or not data:
        return MergedPrCheckResult(kind=MergedPrCheckKind.NONE)

    expected_prefix = f"Implements work for #{issue_number}."
    expected_branch = f"subsched/issue-{issue_number}"
    confirmed: list[int] = []
    numbers: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item["number"])
        except (KeyError, TypeError, ValueError):
            continue
        numbers.append(str(number))
        head_ref = str(item.get("headRefName", ""))
        body = str(item.get("body", ""))
        if body.startswith(expected_prefix) and head_ref == expected_branch:
            confirmed.append(number)

    if len(data) == 1 and len(confirmed) == 1:
        return MergedPrCheckResult(kind=MergedPrCheckKind.CONFIRMED, pr_number=confirmed[0])

    return MergedPrCheckResult(
        kind=MergedPrCheckKind.AMBIGUOUS,
        reason=(
            f"issue #{issue_number} is referenced by merged PR(s) "
            f"{', '.join(numbers) or '(unparseable)'} but the match could not be "
            "confirmed automatically (unexpected body/branch format); review manually "
            "before treating this issue as READY"
        ),
    )


def create_or_get_pull_request(
    task: Task,
    branch_name: str,
    base: str = "main",
    repo: str | None = None,
    verification_summary: str = "",
    env: dict[str, str] | None = None,
    timeout_seconds: float = 60.0,
    *,
    close_issue: bool = False,
) -> PullRequestResult:
    """Idempotently create or retrieve existing PR using gh CLI.

    Unlike a bare PullRequestInfo | None, PullRequestResult also carries a redacted
    `output` describing *why* creation failed (regression test for #128: this was
    previously discarded, making a resulting NEEDS_HUMAN escalation unexplainable).
    """
    check = lookup_existing_pr(
        branch_name,
        issue_number=task.issue_number,
        base=base,
        repo=repo,
        env=env,
        timeout_seconds=timeout_seconds,
    )
    if check.kind is ExistingPrCheckKind.CONFIRMED:
        if check.info is None:
            return PullRequestResult(
                kind=PullRequestResultKind.FAILURE,
                info=None,
                output=_redact(f"existing PR confirmed but info missing: {check.reason}"),
            )
        return PullRequestResult(kind=PullRequestResultKind.SUCCESS, info=check.info)

    if check.kind is ExistingPrCheckKind.AMBIGUOUS:
        return PullRequestResult(
            kind=PullRequestResultKind.FAILURE,
            info=None,
            output=_redact(f"existing PR verification failed: {check.reason}"),
        )

    summary = (
        extract_pr_summary(
            Path(task.worktree), task, base, env=env, timeout_seconds=timeout_seconds
        )
        if task.worktree
        else task.title
    )
    body = build_pr_body(
        issue_number=task.issue_number,
        summary=summary,
        verification_results=verification_summary,
        close_issue=close_issue,
    )
    title = f"{task.title} (#{task.issue_number})"

    argv = [
        "gh",
        "pr",
        "create",
        "--head",
        branch_name,
        "--base",
        base,
        "--title",
        title,
        "--body",
        body,
    ]
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
        if res.returncode != 0:
            output = _redact(f"{res.stdout}\n{res.stderr}".strip())
            return PullRequestResult(kind=PullRequestResultKind.FAILURE, info=None, output=output)
        url = res.stdout.strip()
        match = re.search(r"/pull/(\d+)", url)
        if match is None:
            return PullRequestResult(
                kind=PullRequestResultKind.FAILURE,
                info=None,
                output=_redact(f"could not parse PR number from gh output: {url}"),
            )
        pr_number = int(match.group(1))
        info = PullRequestInfo(number=pr_number, url=url, title=title, body=body)
        return PullRequestResult(kind=PullRequestResultKind.SUCCESS, info=info)
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        return PullRequestResult(
            kind=PullRequestResultKind.FAILURE, info=None, output=_redact(str(error))
        )
