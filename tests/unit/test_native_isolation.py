"""Issue #293 admission regressions; these do not prove runtime containment."""

from pathlib import Path
from subprocess import CompletedProcess, run
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from subsched.agents.codex import CodexApprovalMode
from subsched.agents.native import NativeWorker
from subsched.cli import app
from subsched.config import ConfigError, NativeIsolationConfig, load_config
from subsched.contract import bootstrap_task_files
from subsched.models import AgentResult, AgentResultKind, Issue, Task
from subsched.preflight import PreflightCheckResult, validate_native_preflight

_DIGEST = "sha256:" + "a" * 64
_PROXY_DIGEST = "sha256:" + "b" * 64


def _isolation_yaml(auth_root: Path, *, image: str | None = None) -> str:
    selected_image = image or f"registry.invalid/subsched-worker@{_DIGEST}"
    return (
        "github:\n  repo: acme/widgets\n"
        "agents:\n"
        "  claude:\n    enabled: true\n    priority: 100\n"
        "  codex:\n    enabled: true\n    priority: 90\n"
        "isolation:\n"
        "  backend: container\n"
        "  runtime: docker\n"
        f"  image: {selected_image}\n"
        "  network: subsched-provider-egress\n"
        "  proxy_url: http://subsched-provider-proxy:3128\n"
        f"  proxy_image: registry.invalid/subsched-proxy@{_PROXY_DIGEST}\n"
        "  auth:\n"
        f"    claude: {auth_root / 'claude'}\n"
        f"    codex: {auth_root / 'codex'}\n"
    )


def test_container_isolation_config_is_typed_and_digest_pinned(tmp_path: Path) -> None:
    config_path = tmp_path / "subsched.yaml"
    config_path.write_text(_isolation_yaml(tmp_path / "auth"), encoding="utf-8")

    config = load_config(config_path)

    assert config.isolation.backend == "container"
    assert config.isolation.runtime == "docker"
    assert config.isolation.image == f"registry.invalid/subsched-worker@{_DIGEST}"
    assert config.isolation.network == "subsched-provider-egress"
    assert config.isolation.proxy_url == "http://subsched-provider-proxy:3128"
    assert config.isolation.proxy_image == (
        f"registry.invalid/subsched-proxy@{_PROXY_DIGEST}"
    )
    assert dict(config.isolation.auth)["codex"] == tmp_path / "auth" / "codex"


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("registry.invalid/subsched-worker:latest", "digest"),
        ("registry.invalid/subsched-worker@sha256:abc", "digest"),
        ("-malformed@" + _DIGEST, "image"),
    ],
)
def test_container_isolation_config_rejects_unpinned_or_unsafe_image(
    tmp_path: Path, replacement: str, message: str
) -> None:
    config_path = tmp_path / "subsched.yaml"
    config_path.write_text(_isolation_yaml(tmp_path / "auth", image=replacement), encoding="utf-8")

    with pytest.raises(ConfigError, match=message):
        load_config(config_path)


def test_container_isolation_config_rejects_proxy_credentials(tmp_path: Path) -> None:
    config_path = tmp_path / "subsched.yaml"
    config_path.write_text(
        _isolation_yaml(tmp_path / "auth").replace(
            "http://subsched-provider-proxy:3128",
            "http://user:secret@subsched-provider-proxy:3128",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="proxy_url"):
        load_config(config_path)


def test_container_isolation_config_rejects_relative_auth_path(tmp_path: Path) -> None:
    config_path = tmp_path / "subsched.yaml"
    config_path.write_text(
        _isolation_yaml(tmp_path / "auth").replace(
            str(tmp_path / "auth" / "codex"), "relative/codex"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=r"isolation\.auth\.codex"):
        load_config(config_path)


def test_container_isolation_rejects_shared_concurrent_worker_network(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "subsched.yaml"
    config_path.write_text(
        _isolation_yaml(tmp_path / "auth")
        + "execution:\n  concurrency: 2\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="concurrency"):
        load_config(config_path)


def _runtime_config(auth_root: Path) -> NativeIsolationConfig:
    return NativeIsolationConfig(
        backend="container",
        runtime="docker",
        image=f"registry.invalid/subsched-worker@{_DIGEST}",
        network="subsched-provider-egress",
        proxy_url="http://subsched-provider-proxy:3128",
        proxy_image=f"registry.invalid/subsched-proxy@{_PROXY_DIGEST}",
        auth=(("codex", auth_root),),
    )


def _secure_auth_dir(path: Path) -> Path:
    path.mkdir(mode=0o700)
    (path / "auth.json").write_text("synthetic-auth", encoding="utf-8")
    (path / "auth.json").chmod(0o600)
    return path


def _successful_attestation_output(
    argv: list[str], *, proxy_mounts: str = "[]"
) -> str:
    if argv[1] == "info":
        return '"linux"'
    if argv[1:3] == ["image", "inspect"]:
        if "subsched-proxy" in argv[3]:
            return (
                '{"Id":"sha256:proxy-id","RepoDigests":'
                f'["registry.invalid/subsched-proxy@{_PROXY_DIGEST}"],'
                '"Config":{"Entrypoint":["squid"],"Cmd":null,'
                '"Env":["PATH=/usr/bin"],"User":"proxy"}}'
            )
        return (
            '{"RepoDigests":'
            f'["registry.invalid/subsched-worker@{_DIGEST}"],'
            '"Config":{"Volumes":null}}'
        )
    if argv[1:3] == ["network", "inspect"]:
        return (
            '{"Internal":true,"Containers":{"id":'
            '{"Name":"subsched-provider-proxy"}}}'
        )
    if argv[1:3] == ["container", "inspect"]:
        return (
            '{"State":{"Running":true},"Image":"sha256:proxy-id",'
            '"Config":{"Entrypoint":["squid"],"Cmd":null,'
            '"Env":["PATH=/usr/bin"],"User":"proxy"},'
            '"HostConfig":{"Privileged":false,"ReadonlyRootfs":true,'
            '"CapAdd":null,"CapDrop":["ALL"],"Binds":null,"Devices":[],'
            '"PidMode":"","IpcMode":"private","NetworkMode":"bridge",'
            '"SecurityOpt":["no-new-privileges=true"]},'
            f'"Mounts":{proxy_mounts},"NetworkSettings":{{"Networks":'
            '{"subsched-provider-egress":{},"bridge":{}}}}'
        )
    raise AssertionError(argv)


def test_container_attestation_requires_linux_digest_and_internal_network(
    tmp_path: Path,
) -> None:
    from subsched.agents.isolation import verify_native_isolation

    auth = _secure_auth_dir(tmp_path / "auth")
    calls: list[list[str]] = []

    def run(argv: list[str], **_: object) -> CompletedProcess[str]:
        calls.append(argv)
        return CompletedProcess(argv, 0, _successful_attestation_output(argv), "")

    result = verify_native_isolation(
        _runtime_config(auth),
        enabled_agents=("codex",),
        resolver=lambda _: Path("/usr/bin/docker"),
        run_cmd=run,
    )

    assert result is None
    assert [call[1:3] for call in calls] == [
        ["info", "--format"],
        ["image", "inspect"],
        ["network", "inspect"],
        ["image", "inspect"],
        ["container", "inspect"],
    ]


def test_container_attestation_rejects_proxy_runtime_mount_override(
    tmp_path: Path,
) -> None:
    from subsched.agents.isolation import verify_native_isolation

    auth = _secure_auth_dir(tmp_path / "auth")

    def run_with_mount(argv: list[str], **_: object) -> CompletedProcess[str]:
        mounts = '[{"Source":"/host/policy","Destination":"/etc/squid"}]'
        return CompletedProcess(
            argv, 0, _successful_attestation_output(argv, proxy_mounts=mounts), ""
        )

    failure = verify_native_isolation(
        _runtime_config(auth),
        enabled_agents=("codex",),
        resolver=lambda _: Path("/usr/bin/docker"),
        run_cmd=run_with_mount,
    )

    assert failure is not None
    assert "proxy" in failure


def test_container_attestation_rejects_non_internal_network(tmp_path: Path) -> None:
    from subsched.agents.isolation import verify_native_isolation

    auth = _secure_auth_dir(tmp_path / "auth")

    def run(argv: list[str], **_: object) -> CompletedProcess[str]:
        output = (
            '{"Internal": false}'
            if argv[1:3] == ["network", "inspect"]
            else _successful_attestation_output(argv)
        )
        return CompletedProcess(argv, 0, output, "")

    result = verify_native_isolation(
        _runtime_config(auth),
        enabled_agents=("codex",),
        resolver=lambda _: Path("/usr/bin/docker"),
        run_cmd=run,
    )

    assert result is not None
    assert "internal" in result.casefold()


def test_container_attestation_rejects_symlinked_auth(tmp_path: Path) -> None:
    from subsched.agents.isolation import verify_native_isolation

    real = _secure_auth_dir(tmp_path / "real")
    link = tmp_path / "auth"
    link.symlink_to(real)

    result = verify_native_isolation(
        _runtime_config(link), enabled_agents=("codex",), resolver=lambda _: Path("/usr/bin/docker")
    )

    assert result is not None
    assert "symlink" in result.casefold()


def test_container_attestation_rejects_malformed_nested_proxy_result(
    tmp_path: Path,
) -> None:
    from subsched.agents.isolation import verify_native_isolation

    auth = _secure_auth_dir(tmp_path / "auth")

    def run_malformed(argv: list[str], **_: object) -> CompletedProcess[str]:
        if argv[1:3] == ["container", "inspect"]:
            output = '{"State":[],"Image":"sha256:proxy-id","NetworkSettings":{}}'
        else:
            output = _successful_attestation_output(argv)
        return CompletedProcess(argv, 0, output, "")

    failure = verify_native_isolation(
        _runtime_config(auth),
        enabled_agents=("codex",),
        resolver=lambda _: Path("/usr/bin/docker"),
        run_cmd=run_malformed,
    )

    assert failure is not None
    assert "unverified" in failure


def test_container_request_has_only_explicit_isolated_mounts_and_environment(
    tmp_path: Path,
) -> None:
    from subsched.agents.base import ProcessExecutionRequest
    from subsched.agents.isolation import wrap_native_request

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    auth = _secure_auth_dir(tmp_path / "auth")
    original = ProcessExecutionRequest(
        argv=("codex", "exec", "-"),
        cwd=worktree,
        env={"HOME": "/host/home", "GH_TOKEN": "secret", "PATH": "/host/bin"},
        stdin_payload=b"prompt",
    )

    wrapped = wrap_native_request(
        original,
        agent="codex",
        config=_runtime_config(auth),
        runtime_executable=Path("/usr/bin/docker"),
    )

    joined = " ".join(wrapped.argv)
    assert wrapped.argv[:3] == ("/usr/bin/docker", "run", "--rm")
    assert "--read-only" in wrapped.argv
    assert "--cap-drop" in wrapped.argv and "ALL" in wrapped.argv
    assert "--network" in wrapped.argv and "subsched-provider-egress" in wrapped.argv
    assert f"src={worktree},dst={worktree}" in joined
    assert f"src={auth},dst=/run/subsched-auth,readonly" in joined
    assert "/host/home" not in joined
    assert "GH_TOKEN" not in joined and "secret" not in joined
    assert "HOME=/isolated-home" in joined
    assert "HTTP_PROXY=http://subsched-provider-proxy:3128" in joined
    assert wrapped.stdin_payload == b"prompt"


def test_container_cleanup_force_removes_a_surviving_invocation() -> None:
    from subsched.agents.isolation import cleanup_native_container

    listed = iter(('"subsched-worker-github-293-test"\n', ""))
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append(argv)
        if argv[1:3] == ["container", "ls"]:
            return CompletedProcess(argv, 0, next(listed), "")
        if argv[1:3] == ["container", "rm"]:
            return CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    failure = cleanup_native_container(
        Path("/usr/bin/docker"),
        "subsched-worker-github-293-test",
        env={},
        run_cmd=fake_run,
    )

    assert failure is None
    assert any(call[1:4] == ["container", "rm", "--force"] for call in calls)


def test_container_cleanup_rejects_malformed_listing_before_removal() -> None:
    from subsched.agents.isolation import cleanup_native_container

    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> CompletedProcess[str]:
        calls.append(argv)
        return CompletedProcess(argv, 0, "{}\n", "")

    failure = cleanup_native_container(
        Path("/usr/bin/docker"), "subsched-worker-github-293-test", env={}, run_cmd=fake_run
    )

    assert failure is not None
    assert "invalid" in failure
    assert all(call[1:3] != ["container", "rm"] for call in calls)


def test_task_specific_git_commit_is_imported_without_exposing_common_git(
    tmp_path: Path,
) -> None:
    from subsched.agents.isolation import import_isolated_git, prepare_isolated_git

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run(["git", "init", "--quiet", "-b", "main", str(worktree)], check=True)
    run(["git", "-C", str(worktree), "config", "user.name", "Test"], check=True)
    run(
        ["git", "-C", str(worktree), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    tracked = worktree / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    run(["git", "-C", str(worktree), "add", "tracked.txt"], check=True)
    run(["git", "-C", str(worktree), "commit", "--quiet", "-m", "initial"], check=True)
    original_git_dir = run(
        ["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    context = prepare_isolated_git(worktree, tmp_path / "state", "github-293")
    tracked.write_text("after\n", encoding="utf-8")
    sandbox_env = {
        "GIT_DIR": str(context.git_dir),
        "GIT_WORK_TREE": str(worktree),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    run(["git", "add", "tracked.txt"], check=True, env=sandbox_env)
    run(
        [
            "git",
            "-c",
            "user.name=Worker",
            "-c",
            "user.email=worker@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "worker commit",
        ],
        check=True,
        env=sandbox_env,
    )

    assert import_isolated_git(worktree, context, read_only=False) is None
    assert (
        run(
            ["git", "-C", str(worktree), "log", "-1", "--format=%s"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == "worker commit"
    )
    assert str(context.git_dir) != original_git_dir
    assert tracked.read_text(encoding="utf-8") == "after\n"


def test_read_only_stage_rejects_sandbox_commit(tmp_path: Path) -> None:
    from subsched.agents.isolation import import_isolated_git, prepare_isolated_git

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run(["git", "init", "--quiet", "-b", "main", str(worktree)], check=True)
    run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "--quiet",
            "-m",
            "initial",
        ],
        check=True,
    )
    context = prepare_isolated_git(worktree, tmp_path / "state", "github-293")
    run(
        [
            "git",
            f"--git-dir={context.git_dir}",
            f"--work-tree={worktree}",
            "-c",
            "user.name=Worker",
            "-c",
            "user.email=worker@example.invalid",
            "commit",
            "--allow-empty",
            "--quiet",
            "-m",
            "unexpected",
        ],
        check=True,
    )

    failure = import_isolated_git(worktree, context, read_only=True)

    assert failure is not None
    assert "read-only" in failure


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_dispatch_requires_isolation_even_with_verified_billing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent: str
) -> None:
    # Isolate admission from the existing task-contract and provider-parser tests.
    # No provider or runtime process is launched by this test, even on regression.
    monkeypatch.setattr("subsched.agents.native.validate_dispatch_preconditions", lambda *_: None)
    claude = MagicMock()
    codex = MagicMock()
    claude.execute.return_value = AgentResult(AgentResultKind.PASS)
    codex.execute.return_value = AgentResult(AgentResultKind.PASS)
    worker = NativeWorker(
        claude_agent=claude,
        codex_agent=codex,
        subscription_billing_verified=True,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
    )
    task = Task.from_issue(Issue(number=293, title="Isolation regression"))
    task = task.with_worktree(str(tmp_path))
    preserved = tmp_path / "existing-untracked.txt"
    preserved.write_text("preserve prior work\n", encoding="utf-8")

    result = worker.run(task, agent)

    assert result.kind is AgentResultKind.FAILURE
    assert "isolation" in result.output.casefold()
    claude.execute.assert_not_called()
    codex.execute.assert_not_called()
    assert preserved.read_text(encoding="utf-8") == "preserve prior work\n"
    assert not (tmp_path / ".ai" / "codex-output.schema.json").exists()


def test_native_dispatch_uses_attested_container_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.agents.isolation import IsolationGitContext

    auth = _secure_auth_dir(tmp_path / "auth")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task = Task.from_issue(Issue(number=293, title="Isolation regression")).with_worktree(
        str(worktree)
    )
    bootstrap_task_files(worktree, task)
    codex = MagicMock()
    codex.execute.return_value = AgentResult(AgentResultKind.PASS)
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        "subsched.agents.native.verify_native_isolation", lambda *args, **kwargs: None
    )
    git_context = IsolationGitContext(
        tmp_path / "sandbox.git", "a" * 40, tmp_path / "worktree.git"
    )
    monkeypatch.setattr(
        "subsched.agents.native.prepare_isolated_git",
        lambda *args, **kwargs: git_context,
    )
    monkeypatch.setattr(
        "subsched.agents.native.import_isolated_git", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "subsched.agents.native.cleanup_native_container", lambda *args, **kwargs: None
    )

    def wrap(request: object, **kwargs: object) -> object:
        observed.update(kwargs)
        observed["request"] = request
        return request

    monkeypatch.setattr("subsched.agents.native.wrap_native_request", wrap)
    worker = NativeWorker(
        codex_agent=codex,
        subscription_billing_verified=True,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
        isolation_config=_runtime_config(auth),
        isolation_runtime_executable=Path("/usr/bin/docker"),
        isolation_state_root=tmp_path / "state",
    )

    result = worker.run(task, "codex")

    assert result.kind is AgentResultKind.PASS
    assert observed["agent"] == "codex"
    assert observed["config"] == _runtime_config(auth)
    assert observed["runtime_executable"] == Path("/usr/bin/docker")
    assert observed["git_dir"] == git_context.git_dir
    codex.execute.assert_called_once_with(observed["request"])


def test_native_dispatch_cleans_up_when_adapter_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.agents.isolation import IsolationGitContext

    auth = _secure_auth_dir(tmp_path / "auth")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    task = Task.from_issue(Issue(number=293, title="Isolation regression")).with_worktree(
        str(worktree)
    )
    bootstrap_task_files(worktree, task)
    codex = MagicMock()
    codex.execute.side_effect = RuntimeError("synthetic secret-bearing failure")
    context = IsolationGitContext(
        tmp_path / "sandbox.git", "a" * 40, tmp_path / "worktree.git"
    )
    cleanup_calls: list[str] = []
    monkeypatch.setattr(
        "subsched.agents.native.verify_native_isolation", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "subsched.agents.native.prepare_isolated_git", lambda *args, **kwargs: context
    )
    monkeypatch.setattr(
        "subsched.agents.native.wrap_native_request", lambda request, **kwargs: request
    )
    monkeypatch.setattr(
        "subsched.agents.native.cleanup_native_container",
        lambda runtime, name, **kwargs: cleanup_calls.append(name) or None,
    )
    import_call = MagicMock()
    import_call.return_value = None
    monkeypatch.setattr("subsched.agents.native.import_isolated_git", import_call)
    worker = NativeWorker(
        codex_agent=codex,
        subscription_billing_verified=True,
        codex_approval_mode=CodexApprovalMode.APPROVE_FOR_ME,
        isolation_config=_runtime_config(auth),
        isolation_runtime_executable=Path("/usr/bin/docker"),
        isolation_state_root=tmp_path / "state",
    )

    result = worker.run(task, "codex")

    assert result.kind is AgentResultKind.FAILURE
    assert "secret-bearing" not in result.output
    assert len(cleanup_calls) == 1
    import_call.assert_called_once()


@pytest.fixture
def compatible_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Every CLI is compatible. CLI availability alone cannot attest isolation.
    monkeypatch.setattr("subsched.preflight._default_resolver", lambda name: tmp_path / name)
    monkeypatch.setattr(
        "subsched.preflight.probe_command_capabilities",
        lambda name, executable, **_: PreflightCheckResult(
            name=name, found=True, executable_path=executable, compatible=True
        ),
    )


@pytest.mark.usefixtures("compatible_commands")
@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_preflight_rejects_compatible_commands_without_verified_isolation(agent: str) -> None:
    report = validate_native_preflight(enabled_agents=(agent,))

    assert report.passed is False
    assert any("isolation" in reason.casefold() for reason in report.failure_reasons)
    isolation = report.get("isolation")
    assert isolation is not None
    assert isolation.compatible is False


def test_preflight_reports_attested_container_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = _secure_auth_dir(tmp_path / "auth")
    config = _runtime_config(auth)
    monkeypatch.setattr(
        "subsched.preflight.verify_native_isolation", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "subsched.preflight.probe_command_capabilities",
        lambda name, executable, **kwargs: PreflightCheckResult(
            name=name, found=True, executable_path=executable, compatible=True
        ),
    )

    report = validate_native_preflight(
        enabled_agents=(),
        isolation_config=config,
        executable_resolver=lambda name: Path(f"/usr/bin/{name}"),
        run_cmd=lambda argv, **kwargs: CompletedProcess(argv, 0, "version", ""),
    )

    isolation = report.get("isolation")
    assert isolation is not None
    assert isolation.compatible is True
    assert isolation.executable_path == Path("/usr/bin/docker")
    assert "dedicated provider auth" in isolation.details


@pytest.mark.usefixtures("compatible_commands")
def test_doctor_reports_unverified_isolation_and_worker_credential_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from subsched.github.issues import TokenDiagnosis

    monkeypatch.setattr(
        "subsched.cli.diagnose_token",
        lambda: TokenDiagnosis(
            authenticated=False, scopes=(), can_discover=False, can_write=False, broad_scopes=()
        ),
    )

    result = CliRunner().invoke(app, ["doctor"])

    assert result.exit_code == 1
    assert "isolation" in result.output.casefold()
    assert "worker credential tier" in result.output.casefold()
    assert "unverified" in result.output.casefold()
