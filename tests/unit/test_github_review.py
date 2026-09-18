from __future__ import annotations

import subprocess

import pytest

from subsched.github.review import (
    MAX_COMMENT_CHARS,
    PostCommentResultKind,
    post_pr_comment,
)


def test_post_pr_comment_success_passes_bounded_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    expected_env = {"PATH": "/usr/bin"}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="comment URL\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = post_pr_comment(
        42,
        "review body",
        repo="owner/repo",
        env=expected_env,
        timeout_seconds=7.5,
    )

    assert result.kind is PostCommentResultKind.SUCCESS
    assert result.output == "comment URL"
    assert captured["argv"] == [
        "gh",
        "pr",
        "comment",
        "42",
        "--body",
        "review body",
        "--repo",
        "owner/repo",
    ]
    assert captured["env"] is expected_env
    assert captured["timeout"] == 7.5
    assert captured["check"] is False


def test_post_pr_comment_truncates_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_argv: list[str] = []

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_argv.extend(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    body = "x" * (MAX_COMMENT_CHARS + 1)

    result = post_pr_comment(8, body)

    assert result.kind is PostCommentResultKind.SUCCESS
    posted_body = captured_argv[captured_argv.index("--body") + 1]
    assert posted_body == body[:MAX_COMMENT_CHARS] + "... [truncated]"
    assert "--repo" not in captured_argv


def test_post_pr_comment_redacts_failure_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "github_pat_abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, stdout="", stderr=f"authentication failed for {secret}\n"
        ),
    )

    result = post_pr_comment(9, "body")

    assert result.kind is PostCommentResultKind.FAILURE
    assert "exited 1" in result.output
    assert secret not in result.output
    assert "[REDACTED]" in result.output


@pytest.mark.parametrize(
    "error",
    (FileNotFoundError("gh"), subprocess.TimeoutExpired(cmd="gh", timeout=2)),
)
def test_post_pr_comment_handles_invocation_errors(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    result = post_pr_comment(10, "body")

    assert result.kind is PostCommentResultKind.FAILURE
    assert "could not post PR comment" in result.output
    assert "invocation failed" in result.output
