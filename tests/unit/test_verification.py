from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from subsched.config import NativeIsolationConfig
from subsched.verification import MAX_VERIFICATION_ERROR_CHARS, run_verification


def test_run_verification_all_pass(tmp_path: Path) -> None:
    commands = (
        f"{sys.executable} -c \"print('gate1 ok')\"",
        f"{sys.executable} -c \"print('gate2 ok')\"",
    )
    report = run_verification(tmp_path, commands)
    assert report.passed is True
    assert len(report.gates) == 2
    assert report.gates[0].passed is True
    assert report.gates[1].passed is True
    assert "PASS" in report.summary


def test_run_verification_failure_stops_pipeline(tmp_path: Path) -> None:
    commands = (
        f'{sys.executable} -c "import sys; sys.exit(1)"',
        f"{sys.executable} -c \"print('should not run')\"",
    )
    report = run_verification(tmp_path, commands)
    assert report.passed is False
    assert len(report.gates) == 1
    assert report.gates[0].passed is False
    assert "FAIL" in report.summary


def test_run_verification_reports_command_not_found_clearly(tmp_path: Path) -> None:
    """Regression test for #129: a bare command that isn't on PATH (e.g. `pytest` in a
    uv-managed project without an activated venv) must produce a summary that clearly
    says the command was not found, not just a generic "FAIL (exit 1)" that is
    indistinguishable from a real gate failure."""
    commands = ("subsched-definitely-does-not-exist-abcxyz",)
    report = run_verification(tmp_path, commands)
    assert report.passed is False
    assert report.gates[0].command_not_found is True
    assert "command not found" in report.summary.casefold()


def test_run_verification_redacts_command_not_found_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult

    secret = "github_pat_abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setattr(
        verification,
        "run_process_group",
        lambda request: ProcessExecutionResult(
            exit_code=1,
            stdout="",
            stderr=f"could not execute {secret}",
            command_not_found=True,
        ),
    )

    report = run_verification(tmp_path, ("missing-command",))

    assert "[REDACTED]" in report.summary
    assert secret not in report.summary


def test_run_verification_truncates_command_not_found_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult

    marker = "end-marker"
    monkeypatch.setattr(
        verification,
        "run_process_group",
        lambda request: ProcessExecutionResult(
            exit_code=1,
            stdout="",
            stderr="x" * (MAX_VERIFICATION_ERROR_CHARS + 1) + marker,
            command_not_found=True,
        ),
    )

    report = run_verification(tmp_path, ("missing-command",))

    assert "... [truncated]" in report.summary
    assert marker not in report.summary


@pytest.mark.parametrize("commands", ((), ("   ", "\t")))
def test_run_verification_fails_when_no_executable_gate_runs(
    tmp_path: Path, commands: tuple[str, ...]
) -> None:
    report = run_verification(tmp_path, commands)

    assert report.passed is False
    assert report.gates == ()
    assert report.summary == "FAIL (no executable verification commands configured)"


def test_run_verification_fails_when_exit_zero_but_cleanup_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#373: exit_code=0 with an unconfirmed process-group cleanup must not PASS --
    a leftover verification process is a terminal safety condition, not a green gate."""
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult

    monkeypatch.setattr(
        verification,
        "run_process_group",
        lambda request: ProcessExecutionResult(
            exit_code=0,
            stdout="ok",
            stderr="",
            cleanup_succeeded=False,
        ),
    )

    report = run_verification(tmp_path, (f"{sys.executable} -c 'print(1)'",))

    assert report.passed is False
    assert report.gates[0].passed is False
    assert report.gates[0].cleanup_succeeded is False
    assert report.cleanup_confirmed is False
    assert "cleanup" in report.summary.casefold()


def test_run_verification_confirms_cleanup_on_pass(tmp_path: Path) -> None:
    commands = (f"{sys.executable} -c \"print('gate ok')\"",)
    report = run_verification(tmp_path, commands)

    assert report.passed is True
    assert report.gates[0].cleanup_succeeded is True
    assert report.cleanup_confirmed is True


def test_run_verification_uses_networkless_container_when_isolation_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult

    _verification_repo(tmp_path)
    requests = []
    cleanup_calls: list[tuple[Path, str, dict[str, str]]] = []

    def fake_run(request: object) -> ProcessExecutionResult:
        requests.append(request)
        return ProcessExecutionResult(exit_code=0, stdout="ok", stderr="")

    def fake_cleanup(
        runtime: Path, container_name: str, *, env: dict[str, str]
    ) -> None:
        cleanup_calls.append((runtime, container_name, env))

    monkeypatch.setattr(verification, "run_process_group", fake_run)
    monkeypatch.setattr(verification, "cleanup_native_container", fake_cleanup)
    monkeypatch.setattr(verification, "native_container_name", lambda _: "verification-test")
    config = NativeIsolationConfig(
        backend="container",
        runtime="docker",
        image="registry.invalid/worker@sha256:" + "a" * 64,
        network="provider-network",
        proxy_url="http://provider-proxy:3128",
        auth=(("claude", Path("/host/provider-auth")),),
    )

    report = run_verification(
        tmp_path,
        ("python -m pytest tests/unit",),
        env={"PATH": "/usr/bin", "HOME": "/host/home", "SECRET_TOKEN": "do-not-pass"},
        isolation_config=config,
        isolation_runtime_executable=Path("/usr/bin/docker"),
    )

    assert report.passed is True
    assert len(requests) == 1
    request = requests[0]
    assert request.argv[:3] == ("/usr/bin/docker", "run", "--rm")
    assert request.argv[request.argv.index("--network") + 1] == "none"
    assert "HOME=/isolated-home" in request.argv
    assert "SECRET_TOKEN" not in " ".join(request.argv)
    assert "provider-network" not in request.argv
    assert "http://provider-proxy:3128" not in request.argv
    assert "/host/provider-auth" not in " ".join(request.argv)
    mounts = [
        request.argv[index + 1]
        for index, value in enumerate(request.argv)
        if value == "--mount"
    ]
    assert mounts[0] == f"type=bind,src={tmp_path},dst={tmp_path}"
    assert len(mounts) == 3
    assert "dst=/run/subsched-git" in mounts[1]
    assert f"dst={tmp_path}/.git" in mounts[2]
    assert all(f"src={tmp_path}/.git" not in mount for mount in mounts[1:])
    assert request.argv[-4:] == ("python", "-m", "pytest", "tests/unit")
    assert request.env == {"PATH": "/usr/bin"}
    assert cleanup_calls == [(Path("/usr/bin/docker"), "verification-test", request.env)]


def test_run_verification_fails_closed_when_container_cleanup_is_unconfirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult

    monkeypatch.setattr(
        verification,
        "run_process_group",
        lambda request: ProcessExecutionResult(exit_code=0, stdout="ok", stderr=""),
    )
    _verification_repo(tmp_path)
    monkeypatch.setattr(
        verification,
        "cleanup_native_container",
        lambda *args, **kwargs: "verification container cleanup could not be confirmed",
    )
    config = NativeIsolationConfig(
        backend="container",
        runtime="docker",
        image="registry.invalid/worker@sha256:" + "a" * 64,
    )

    report = run_verification(
        tmp_path,
        ("true",),
        isolation_config=config,
        isolation_runtime_executable=Path("/usr/bin/docker"),
    )

    assert report.passed is False
    assert report.cleanup_confirmed is False
    assert report.gates[0].cleanup_succeeded is False


def test_run_verification_fails_closed_when_container_runtime_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verification

    monkeypatch.setattr(
        verification,
        "run_process_group",
        lambda request: pytest.fail("verification must not fall back to host execution"),
    )
    config = NativeIsolationConfig(
        backend="container",
        runtime="docker",
        image="registry.invalid/worker@sha256:" + "a" * 64,
    )

    report = run_verification(tmp_path, ("true",), isolation_config=config)

    assert report.passed is False
    assert report.gates[0].stderr == (
        "verification container runtime was not supplied by preflight"
    )


def test_scheduler_does_not_finalize_when_no_verification_gate_runs(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from subsched.models import (
        AgentResult,
        AgentResultKind,
        Capacity,
        CapacityState,
        Issue,
        TaskState,
    )
    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker
    from subsched.storage import JsonStateStore

    store = JsonStateStore(tmp_path / "state.json")
    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker(
            {(101, "claude"): (AgentResult(AgentResultKind.PASS),)}
        ),
        worktree_root=tmp_path / "worktrees",
        verification_commands=(),
        max_verification_failures=1,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    scheduler.tick(
        [
            Capacity(
                agent="claude",
                state=CapacityState.AVAILABLE,
                observed_at=datetime.now(UTC),
                source="provider",
                confidence="high",
            )
        ]
    )

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.verification_failures == 1
    assert task.pr is None


def test_scheduler_passes_configured_verification_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verif_mod
    from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue
    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker
    from subsched.storage import JsonStateStore

    passed_commands: list[tuple[str, ...]] = []

    def mock_run_verification(
        worktree: Path, commands: tuple[str, ...], **_: object
    ) -> object:
        passed_commands.append(commands)
        from subsched.verification import VerificationReport

        return VerificationReport(passed=True, gates=(), summary="PASS")

    monkeypatch.setattr(verif_mod, "run_verification", mock_run_verification)

    store = JsonStateStore(tmp_path / "state.json")
    router = Router([AgentConfig("claude", priority=100)])
    worker = ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)})

    custom_commands = ("echo 'custom-1'", "echo 'custom-2'")
    scheduler = Scheduler(
        store=store,
        router=router,
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        verification_commands=custom_commands,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    from datetime import UTC, datetime

    cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=datetime.now(UTC),
        source="provider",
        confidence="high",
    )
    scheduler.tick([cap])

    assert len(passed_commands) == 1
    assert passed_commands[0] == custom_commands


def test_scheduler_passes_container_isolation_to_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verif_mod
    from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue
    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker
    from subsched.storage import JsonStateStore

    passed_isolation: list[tuple[NativeIsolationConfig | None, Path | None]] = []

    def mock_run_verification(
        worktree: Path, commands: tuple[str, ...], **kwargs: object
    ) -> object:
        passed_isolation.append(
            (
                kwargs.get("isolation_config"),  # type: ignore[arg-type]
                kwargs.get("isolation_runtime_executable"),  # type: ignore[arg-type]
            )
        )
        from subsched.verification import VerificationReport

        return VerificationReport(passed=True, gates=(), summary="PASS")

    monkeypatch.setattr(verif_mod, "run_verification", mock_run_verification)
    isolation = NativeIsolationConfig(
        backend="container",
        runtime="docker",
        image="registry.invalid/worker@sha256:" + "a" * 64,
    )
    scheduler = Scheduler(
        store=JsonStateStore(tmp_path / "state.json"),
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        isolation_config=isolation,
        isolation_runtime_executable=Path("/usr/bin/docker"),
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    from datetime import UTC, datetime

    scheduler.tick(
        [
            Capacity(
                agent="claude",
                state=CapacityState.AVAILABLE,
                observed_at=datetime.now(UTC),
                source="provider",
                confidence="high",
            )
        ]
    )

    assert passed_isolation == [(isolation, Path("/usr/bin/docker"))]


def test_scheduler_passes_configured_verification_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verif_mod
    from subsched.models import AgentResult, AgentResultKind, Capacity, CapacityState, Issue
    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker
    from subsched.storage import JsonStateStore

    seen_timeouts: list[float] = []

    def mock_run_verification(
        worktree: Path, commands: tuple[str, ...], **kwargs: object
    ) -> object:
        seen_timeouts.append(float(kwargs["timeout_seconds"]))  # type: ignore[arg-type]
        from subsched.verification import VerificationReport

        return VerificationReport(passed=True, gates=(), summary="PASS")

    monkeypatch.setattr(verif_mod, "run_verification", mock_run_verification)

    store = JsonStateStore(tmp_path / "state.json")
    router = Router([AgentConfig("claude", priority=100)])
    worker = ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)})

    scheduler = Scheduler(
        store=store,
        router=router,
        worker=worker,
        worktree_root=tmp_path / "worktrees",
        verification_timeout_seconds=45.0,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    from datetime import UTC, datetime

    cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=datetime.now(UTC),
        source="provider",
        confidence="high",
    )
    scheduler.tick([cap])

    assert seen_timeouts == [45.0]


def test_scheduler_verification_cleanup_unconfirmed_escalates_without_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#373: an unconfirmed verification-process cleanup must escalate straight to
    NEEDS_HUMAN -- it must not consume the verification failure budget and must not
    re-dispatch the task as if it were an ordinary gate failure."""
    import subsched.verification as verif_mod
    from subsched.models import (
        AgentResult,
        AgentResultKind,
        Capacity,
        CapacityState,
        Issue,
        TaskState,
    )
    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker
    from subsched.storage import JsonStateStore
    from subsched.verification import GateResult, VerificationReport

    def mock_run_verification(
        worktree: Path, commands: tuple[str, ...], **_: object
    ) -> VerificationReport:
        return VerificationReport(
            passed=False,
            gates=(
                GateResult(
                    command="quality gate",
                    exit_code=0,
                    stdout="",
                    stderr="",
                    passed=False,
                    cleanup_succeeded=False,
                ),
            ),
            summary="quality gate: FAIL (process cleanup unconfirmed)",
            cleanup_confirmed=False,
        )

    monkeypatch.setattr(verif_mod, "run_verification", mock_run_verification)

    store = JsonStateStore(tmp_path / "state.json")
    scheduler = Scheduler(
        store=store,
        router=Router([AgentConfig("claude", priority=100)]),
        worker=ScriptedWorker({(101, "claude"): (AgentResult(AgentResultKind.PASS),)}),
        worktree_root=tmp_path / "worktrees",
        verification_commands=("true",),
        max_verification_failures=5,
    )
    scheduler.discover([Issue(number=101, title="Task 101")])

    from datetime import UTC, datetime

    cap = Capacity(
        agent="claude",
        state=CapacityState.AVAILABLE,
        observed_at=datetime.now(UTC),
        source="provider",
        confidence="high",
    )
    scheduler.tick([cap])

    task = scheduler.tasks[0]
    assert task.status is TaskState.NEEDS_HUMAN
    assert task.verification_failures == 0
    assert task.needs_human_reason_code == "operator_decision_required"
    assert task.pr is None

    # No re-dispatch: the terminal safety state must hold on a later tick.
    assert scheduler.tick([cap]) is False


def test_scheduler_rejects_non_positive_verification_timeout(tmp_path: Path) -> None:
    from subsched.router import AgentConfig, Router
    from subsched.scheduler import Scheduler, ScriptedWorker
    from subsched.storage import JsonStateStore

    with pytest.raises(ValueError, match="verification_timeout_seconds"):
        Scheduler(
            store=JsonStateStore(tmp_path / "state.json"),
            router=Router([AgentConfig("claude", priority=100)]),
            worker=ScriptedWorker({}),
            worktree_root=tmp_path / "worktrees",
            verification_timeout_seconds=0,
        )


def _verification_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "topic", str(path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "initial",
        ],
        check=True,
    )


@pytest.mark.parametrize("linked", [False, True])
def test_verification_git_snapshot_is_independent(tmp_path: Path, linked: bool) -> None:
    from subsched.agents.isolation import prepare_verification_git

    repo = tmp_path / "repo"
    _verification_repo(repo)
    worktree = repo
    if linked:
        worktree = tmp_path / "linked"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "task", str(worktree)],
            check=True,
        )
    (repo / ".git/hooks/host-only").write_text("synthetic-host-hook\n")
    subprocess.run(
        ["git", "-C", str(repo), "config", "credential.helper", "synthetic-host-credential-helper"],
        check=True,
    )
    before = {
        str(p.relative_to(repo)): p.read_bytes() for p in (repo / ".git").rglob("*") if p.is_file()
    }
    context = prepare_verification_git(worktree, tmp_path / "snapshot")
    assert context.git_dir != repo / ".git"
    assert not (context.git_dir / "hooks/host-only").exists()
    assert "synthetic-host-credential-helper" not in (context.git_dir / "config").read_text()
    assert context.worktree_git_mount.is_file() == linked
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    args = ["git", f"--git-dir={context.git_dir}", f"--work-tree={worktree}"]
    result = subprocess.run(
        [*args, "status", "--porcelain"], env=env, capture_output=True, text=True, check=True
    )
    assert result.stdout == ""
    subprocess.run([*args, "config", "verification.canary", "snapshot-only"], check=True)
    assert not (context.git_dir / "objects/info/alternates").exists()
    assert not (context.git_dir / "commondir").exists()
    assert {
        str(p.relative_to(repo)): p.read_bytes() for p in (repo / ".git").rglob("*") if p.is_file()
    } == before


@pytest.mark.parametrize("linked", [False, True])
def test_verification_snapshot_excludes_unrelated_branch_objects(
    tmp_path: Path, linked: bool
) -> None:
    from subsched.agents.isolation import prepare_verification_git

    repo = tmp_path / "repo"
    _verification_repo(repo)
    subprocess.run(["git", "-C", str(repo), "switch", "-qc", "unrelated"], check=True)
    (repo / "unrelated.txt").write_text("synthetic unrelated canary\n")
    subprocess.run(["git", "-C", str(repo), "add", "unrelated.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "unrelated",
        ],
        check=True,
    )
    unrelated = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(["git", "-C", str(repo), "switch", "-q", "topic"], check=True)
    worktree = repo
    if linked:
        worktree = tmp_path / "linked"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "task", str(worktree)],
            check=True,
        )
    context = prepare_verification_git(worktree, tmp_path / "snapshot")
    result = subprocess.run(
        ["git", f"--git-dir={context.git_dir}", "cat-file", "-e", unrelated],
        capture_output=True,
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    "kind",
    [
        "missing",
        "gitfile",
        "symlink",
        "gitdir-symlink",
        "objects-symlink",
        "alternates",
        "commondir",
    ],
)
def test_verification_rejects_unsafe_git_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import subsched.verification as verification

    repo = tmp_path / "repo"
    _verification_repo(repo)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    if kind == "gitfile":
        (worktree / ".git").write_text("gitdir: /missing/gitdir\n")
    elif kind == "symlink":
        (worktree / ".git").symlink_to(repo / ".git")
    elif kind == "gitdir-symlink":
        (tmp_path / "alias").symlink_to(repo / ".git", target_is_directory=True)
        (worktree / ".git").write_text(f"gitdir: {tmp_path}/alias\n")
    elif kind == "objects-symlink":
        (repo / ".git/objects/canary").symlink_to(tmp_path / "outside")
        worktree = repo
    elif kind == "commondir":
        (repo / ".git/commondir").write_text("/outside\n")
        worktree = repo
    elif kind == "alternates":
        (repo / ".git/objects/info/alternates").write_text("/outside/objects\n")
        worktree = repo
    monkeypatch.setattr(
        verification,
        "run_process_group",
        lambda request: pytest.fail("unsafe metadata must block execution"),
    )
    report = run_verification(
        worktree,
        ("true",),
        isolation_config=NativeIsolationConfig(
            backend="container", runtime="docker", image="worker"
        ),
        isolation_runtime_executable=Path("/usr/bin/docker"),
    )
    assert not report.passed
    assert "Git" in report.gates[0].stderr


def test_verification_rejects_nonregular_linkage_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.agents.isolation import prepare_verification_git

    repo = tmp_path / "repo"
    _verification_repo(repo)
    worktree = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "task", str(worktree)],
        check=True,
    )
    common = repo / ".git/worktrees/linked/commondir"
    common.unlink()
    os.mkfifo(common)
    original_read = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == common:
            pytest.fail("nonregular Git linkage must be rejected before opening")
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    with pytest.raises(ValueError, match="Git"):
        prepare_verification_git(worktree, tmp_path / "snapshot")


@pytest.mark.parametrize(
    ("result_options", "expected"),
    [
        ({"timed_out": True}, "FAIL (timeout)"),
        ({"output_limit_exceeded": True}, "FAIL (output limit exceeded)"),
        ({"cleanup_succeeded": False}, "FAIL (process cleanup unconfirmed)"),
    ],
)
def test_verification_distinguishes_failed_process_outcomes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result_options: dict[str, bool], expected: str
) -> None:
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult

    monkeypatch.setattr(
        verification, "run_process_group",
        lambda request: ProcessExecutionResult(exit_code=1, stdout="", **result_options),
    )
    report = run_verification(tmp_path, ("safe-command",))
    assert not report.passed
    assert expected in report.summary


def test_verification_reports_known_gate_stage_without_raw_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult

    canary = "PRIVATE_ARBITRARY_CANARY"
    output = (
        "=== Running Ruff linter ===\nAll checks passed!\n"
        "=== Running Pytest with Coverage Quality Gate (>=80% line, >=80% branch) ===\n"
        f"{canary}\nFAILED tests/unit/test_{canary}.py\n"
    )
    monkeypatch.setattr(
        verification, "run_process_group",
        lambda request: ProcessExecutionResult(exit_code=1, stdout=output, stderr=canary),
    )
    report = run_verification(tmp_path, ("bash scripts/quality_gate.sh",))
    assert not report.passed
    assert "reported stage: pytest/coverage" in report.summary
    assert canary not in report.summary


@pytest.mark.parametrize(
    ("command", "label"),
    [("pytest tests/unit", "pytest"), ("uv run --frozen mypy src", "mypy"),
     ("uv run ruff check .", "ruff")],
)
def test_verification_identifies_direct_tool_without_echoing_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, label: str
) -> None:
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult

    monkeypatch.setattr(
        verification, "run_process_group",
        lambda request: ProcessExecutionResult(exit_code=1, stdout="PRIVATE_CANARY"),
    )
    report = run_verification(tmp_path, (command,))
    assert f"invoked tool: {label}" in report.summary
    assert "PRIVATE_CANARY" not in report.summary


def test_verification_summary_omits_arbitrary_command_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult

    monkeypatch.setattr(
        verification, "run_process_group",
        lambda request: ProcessExecutionResult(exit_code=1, stdout=""),
    )
    report = run_verification(tmp_path, ("pytest -k PRIVATE_COMMAND_CANARY",))
    assert "gate 1" in report.summary
    assert "PRIVATE_COMMAND_CANARY" not in report.summary


def test_missing_command_diagnostic_omits_unstructured_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subsched.verification as verification
    from subsched.agents.base import ProcessExecutionResult

    monkeypatch.setattr(
        verification, "run_process_group",
        lambda request: ProcessExecutionResult(
            exit_code=1, stdout="", stderr="PRIVATE_STDERR_CANARY", command_not_found=True
        ),
    )
    report = run_verification(tmp_path, ("missing-command",))
    assert "command not found" in report.summary
    assert "PRIVATE_STDERR_CANARY" not in report.summary
