from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# #147: this repo (or an ambient shell's cwd) is itself a git repository, and this
# repository's own worktrees are sometimes used to dogfood subsched (see
# docs/WORKFLOW.md). e2e tests create real, independent git repositories under
# pytest's isolated `tmp_path` -- but a real incident showed the *primary* repository's
# `.git` state (core.bare, user identity, worktree registrations) getting corrupted by
# an e2e test run, by an as-yet-unconfirmed mechanism. Until the root cause is
# conclusively identified, this is a hard, fail-loud safety net: snapshot the real
# repository's `.git` state before every e2e test and assert it is byte-for-byte
# unchanged afterward. If a test (any test, not just ones we suspect) mutates it, the
# suite fails immediately with a clear diagnostic instead of silently leaving the real
# repository in a broken state for a human to discover later.
#
# Caveat: this repo's own worktrees (.ai/worktrees/*) are sometimes used to dogfood
# subsched per docs/WORKFLOW.md. Running this e2e suite while a concurrent `subsched
# run` (or a human) legitimately adds/prunes a worktree or commits on this same primary
# repo will trip this fixture as a false positive -- that's an accepted tradeoff for an
# interim safety net, not a fresh repro of #147. Run the e2e suite with no concurrent
# subsched/git activity against this checkout.

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git_snapshot(repo_root: Path) -> dict[str, str] | None:
    """Best-effort snapshot of `.git` state that must never change from an unrelated
    test run. Returns None (skip the check) if `repo_root` is not actually a git
    repository -- e.g. when this test suite is packaged/vendored elsewhere without a
    `.git` directory, the safety net has nothing to protect and must not itself fail.
    """
    # Deliberately NOT `--is-inside-work-tree`: that check itself fails once
    # core.bare=true (the exact corruption this snapshot exists to catch), which would
    # make this function silently return None -- skipping the very check it's meant to
    # perform -- right when it matters most. `--git-dir` only resolves the `.git`
    # location and keeps working regardless of core.bare.
    probe = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return None

    def _config(key: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", key],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    worktrees = subprocess.run(
        ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    # #147 code review: the reported incident also included two spurious empty commits
    # landed on a real deliverable branch (`subsched/issue-127`) checked out in a
    # *different* worktree than `repo_root` itself -- core.bare/user.*/worktree_list
    # alone would miss that. Branch tip commits are shared refs stored once in the
    # common `.git` dir regardless of which worktree has them checked out, so
    # for-each-ref catches an unexpected new commit on any local branch, not just
    # repo_root's own currently-checked-out HEAD.
    refs = subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "for-each-ref",
            "--format=%(refname) %(objectname)",
            "refs/heads/",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "core.bare": _config("core.bare"),
        "user.name": _config("user.name"),
        "user.email": _config("user.email"),
        "worktree_list": worktrees.stdout if worktrees.returncode == 0 else "<error>",
        "branch_refs": refs.stdout if refs.returncode == 0 else "<error>",
    }


@pytest.fixture(autouse=True)
def _protect_primary_repository_git_state() -> None:
    before = _git_snapshot(_REPO_ROOT)
    yield
    if before is None:
        return
    after = _git_snapshot(_REPO_ROOT)
    if after != before:
        changed = {
            key: (before.get(key), after.get(key))
            for key in before
            if before.get(key) != after.get(key)
        }
        pytest.fail(
            "e2e test run mutated the PRIMARY repository's real .git state at "
            f"{_REPO_ROOT} -- this must never happen (see #147). Changed keys: {changed!r}. "
            "This repository's .git state was NOT automatically restored; inspect and "
            "repair it manually before trusting further git operations here.",
            pytrace=False,
        )
