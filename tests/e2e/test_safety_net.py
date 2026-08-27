from __future__ import annotations

import subprocess
from pathlib import Path

from conftest import _git_snapshot


def _init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.invalid"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )


def test_git_snapshot_returns_none_for_non_git_directory(tmp_path: Path) -> None:
    """Regression test for #147: the safety net must not itself fail (or falsely
    protect nothing silently) on a directory that isn't a git repository at all."""
    assert _git_snapshot(tmp_path) is None


def test_git_snapshot_detects_core_bare_change(tmp_path: Path) -> None:
    """The exact corruption reported in #147: core.bare flipped to true."""
    _init(tmp_path)
    before = _git_snapshot(tmp_path)
    assert before is not None

    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "core.bare", "true"],
        check=True,
        capture_output=True,
    )
    after = _git_snapshot(tmp_path)
    assert after != before
    assert after is not None
    assert after["core.bare"] == "true"


def test_git_snapshot_detects_user_identity_change(tmp_path: Path) -> None:
    """The exact corruption reported in #147: [user] overwritten to test identity."""
    _init(tmp_path)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Real Human"],
        check=True,
        capture_output=True,
    )
    before = _git_snapshot(tmp_path)

    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    after = _git_snapshot(tmp_path)
    assert after != before


def test_git_snapshot_detects_phantom_worktree_registration(tmp_path: Path) -> None:
    """The exact corruption reported in #147: an unrelated pytest tmp_path registered
    in `git worktree list`."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    before = _git_snapshot(repo)

    phantom = tmp_path / "phantom-worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", "phantom", str(phantom)],
        check=True,
        capture_output=True,
    )
    after = _git_snapshot(repo)
    assert after != before
    assert after is not None
    assert "phantom" in after["worktree_list"]


def test_git_snapshot_detects_spurious_commit_on_a_branch_checked_out_elsewhere(
    tmp_path: Path,
) -> None:
    """Regression test for #147 code review: the reported incident included two
    spurious empty commits landed on a real deliverable branch checked out in a
    *different* worktree than the one being snapshotted. core.bare/user.*/worktree_list
    alone would miss this -- branch tip commits must also be tracked, since they are
    shared refs in the common .git dir regardless of which worktree has them checked
    out. This test exercises that exact shape: the spurious commit is made from a
    second, separate worktree, while the snapshot is taken of the primary repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init(repo)
    subprocess.run(
        ["git", "-C", str(repo), "branch", "feature"], check=True, capture_output=True
    )
    other_worktree = tmp_path / "other-worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", str(other_worktree), "feature"],
        check=True,
        capture_output=True,
    )

    before = _git_snapshot(repo)

    # Commit made from the *other* worktree, not `repo` itself.
    subprocess.run(
        ["git", "-C", str(other_worktree), "commit", "--allow-empty", "-q", "-m", "spurious"],
        check=True,
        capture_output=True,
    )
    after = _git_snapshot(repo)
    assert after != before
    assert after is not None
    assert before is not None
    # branch_refs is what actually catches this: it reads refs/heads/feature's tip
    # directly, independent of which worktree (if any) has it checked out.
    # (worktree_list's own HEAD line for other_worktree also changes here, since git
    # reports each worktree's current HEAD commit there too -- both keys legitimately
    # move together; the point is branch_refs is *not* a redundant/no-op addition.)
    assert after["branch_refs"] != before["branch_refs"]


def test_git_snapshot_detects_spurious_commit_on_a_branch_with_no_worktree(
    tmp_path: Path,
) -> None:
    """Complementary case to the one above: a spurious commit lands on a branch that
    has NO worktree checked out anywhere at all (just a plain `git branch` ref).
    worktree_list cannot possibly catch this -- it only lists registered worktrees --
    so this specifically proves branch_refs is load-bearing, not redundant."""
    _init(tmp_path)
    subprocess.run(
        ["git", "-C", str(tmp_path), "branch", "feature"], check=True, capture_output=True
    )
    before = _git_snapshot(tmp_path)
    assert before is not None

    # commit-tree alone doesn't move the branch ref -- move it explicitly via
    # update-ref below, simulating an out-of-band write to refs/heads/feature without
    # ever checking that branch out anywhere.
    commit_result = subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "commit-tree",
            "-p",
            "feature",
            "-m",
            "spurious",
            "feature^{tree}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "update-ref",
            "refs/heads/feature",
            commit_result.stdout.strip(),
        ],
        check=True,
        capture_output=True,
    )

    after = _git_snapshot(tmp_path)
    assert after != before
    assert after is not None
    assert after["worktree_list"] == before["worktree_list"]
    assert after["branch_refs"] != before["branch_refs"]


def test_git_snapshot_stable_across_reads_with_no_changes(tmp_path: Path) -> None:
    _init(tmp_path)
    first = _git_snapshot(tmp_path)
    second = _git_snapshot(tmp_path)
    assert first == second
