from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from subsched.gitenv import git_safe_env
from subsched.storage import secure_directory


class WorktreeError(RuntimeError):
    pass


class InvalidRepositoryError(WorktreeError):
    pass


class WorktreeSecurityError(WorktreeError):
    pass


class WorktreeConflictError(WorktreeError):
    pass


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class WorktreeContext:
    path: Path
    branch: str
    issue_number: int


class WorktreeAdapter(Protocol):
    def prepare_worktree(
        self, issue_number: int, *, branch: str | None = None
    ) -> WorktreeContext: ...

    def validate_worktree_path(self, issue_number: int, path: Path) -> None: ...

    def get_worktree_path(self, issue_number: int) -> Path: ...


class GitWorktreeAdapter:
    def __init__(
        self,
        repo_root: Path,
        worktree_root: Path,
        *,
        run: RunCommand = subprocess.run,
    ) -> None:
        self.repo_root = repo_root.resolve()
        self.worktree_root = worktree_root.absolute()
        if self.worktree_root.is_symlink():
            raise WorktreeSecurityError(
                f"worktree root {self.worktree_root} is a symlink"
            )
        self.run = run
        self._validate_repo()

    def _validate_repo(self) -> None:
        if not self.repo_root.is_dir():
            raise InvalidRepositoryError(f"repository root {self.repo_root} is not a directory")
        try:
            result = self.run(
                ["git", "-C", str(self.repo_root), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                check=False,
                env=git_safe_env(),
            )
        except OSError as error:
            raise InvalidRepositoryError(
                f"failed to execute git in {self.repo_root}"
            ) from error
        if result.returncode != 0 or result.stdout.strip() != "true":
            raise InvalidRepositoryError(
                f"{self.repo_root} is not a valid git repository"
            )

    def get_branch_name(self, issue_number: int) -> str:
        if issue_number <= 0:
            raise ValueError(f"invalid issue number {issue_number}")
        return f"subsched/issue-{issue_number}"

    def get_worktree_path(self, issue_number: int) -> Path:
        if issue_number <= 0:
            raise ValueError(f"invalid issue number {issue_number}")
        return self.worktree_root / f"issue-{issue_number}"

    def validate_worktree_path(self, issue_number: int, path: Path) -> None:
        if issue_number <= 0:
            raise ValueError(f"invalid issue number {issue_number}")
        expected = self.get_worktree_path(issue_number)
        if path.resolve() != expected.resolve() and path != expected:
            raise WorktreeSecurityError(
                f"worktree path {path} does not match expected {expected}"
            )
        if path.is_symlink():
            raise WorktreeSecurityError(f"worktree path {path} is a symlink")
        resolved = path.resolve()
        if not resolved.is_relative_to(self.worktree_root):
            raise WorktreeSecurityError(
                f"worktree path {resolved} escapes root {self.worktree_root}"
            )

    def _registered_worktree_branch(self, path: Path) -> tuple[bool, str | None]:
        result = self.run(
            ["git", "-C", str(self.repo_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
            env=git_safe_env(),
        )
        if result.returncode != 0:
            return False, None
        for record in result.stdout.split("\n\n"):
            lines = record.splitlines()
            worktree_line = next(
                (line for line in lines if line.startswith("worktree ")), None
            )
            if worktree_line is None:
                continue
            registered_path = worktree_line[len("worktree ") :].strip()
            if Path(registered_path).resolve() != path.resolve():
                continue
            branch_line = next(
                (line for line in lines if line.startswith("branch ")), None
            )
            if branch_line is None:
                return True, None
            return True, branch_line[len("branch ") :].strip()
        return False, None

    def _branch_exists(self, branch: str) -> bool:
        result = self.run(
            [
                "git",
                "-C",
                str(self.repo_root),
                "show-ref",
                "--verify",
                "--quiet",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=git_safe_env(),
        )
        return result.returncode == 0

    def prepare_worktree(
        self, issue_number: int, *, branch: str | None = None
    ) -> WorktreeContext:
        target_branch = branch or self.get_branch_name(issue_number)
        target_path = self.get_worktree_path(issue_number)

        self.validate_worktree_path(issue_number, target_path)
        secure_directory(self.worktree_root)

        if target_path.exists():
            if not target_path.is_dir():
                raise WorktreeConflictError(
                    f"path {target_path} exists and is not a directory"
                )
            registered, registered_branch = self._registered_worktree_branch(target_path)
            if registered:
                expected_branch = f"refs/heads/{target_branch}"
                if registered_branch is None:
                    raise WorktreeConflictError(
                        f"registered worktree {target_path} is at detached HEAD"
                    )
                if registered_branch != expected_branch:
                    raise WorktreeConflictError(
                        f"registered worktree {target_path} is on {registered_branch}; "
                        f"expected branch {target_branch}"
                    )
                return WorktreeContext(
                    path=target_path.resolve(),
                    branch=target_branch,
                    issue_number=issue_number,
                )
            raise WorktreeConflictError(
                f"directory {target_path} exists but is not registered as a git worktree"
            )

        if self._branch_exists(target_branch):
            cmd = [
                "git",
                "-C",
                str(self.repo_root),
                "worktree",
                "add",
                str(target_path),
                target_branch,
            ]
        else:
            cmd = [
                "git",
                "-C",
                str(self.repo_root),
                "worktree",
                "add",
                "-b",
                target_branch,
                str(target_path),
                "HEAD",
            ]

        result = self.run(cmd, capture_output=True, text=True, check=False, env=git_safe_env())
        if result.returncode != 0:
            raise WorktreeConflictError(
                f"failed to create git worktree at {target_path}: {result.stderr.strip()}"
            )

        return WorktreeContext(
            path=target_path.resolve(),
            branch=target_branch,
            issue_number=issue_number,
        )
