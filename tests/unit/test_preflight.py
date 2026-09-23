from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

import pytest

from subsched.agents.codex import CodexApprovalMode
from subsched.preflight import (
    PreflightCheckResult,
    PreflightReport,
    probe_command_capabilities,
    probe_subscription_authentication,
    validate_native_preflight,
)

FIXTURES = Path(__file__).parents[1] / "fixtures"


@pytest.mark.parametrize(
    ("agent", "stdout", "stderr"),
    (
        ("codex", "", "Logged in using ChatGPT\n"),
        (
            "claude",
            '{"loggedIn":true,"authMethod":"claude.ai",'
            '"apiProvider":"firstParty","subscriptionType":"pro"}',
            "",
        ),
        (
            "claude",
            '{"loggedIn":true,"authMethod":"oauth_token",'
            '"apiProvider":"firstParty"}',
            "",
        ),
    ),
)
def test_probe_subscription_authentication_accepts_subscription_login(
    tmp_path: Path, agent: str, stdout: str, stderr: str
) -> None:
    executable = tmp_path / agent
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    auth_dir = tmp_path / f"{agent}-auth"
    auth_dir.mkdir()
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=stderr)

    result = probe_subscription_authentication(
        agent, executable, auth_dir=auth_dir, run_cmd=fake_run
    )

    assert result.compatible is True
    assert result.error is None
    assert captured["argv"] == (
        [str(executable.resolve()), "login", "status"]
        if agent == "codex"
        else [str(executable.resolve()), "auth", "status", "--json"]
    )
    env = captured["env"]
    assert isinstance(env, dict)
    home = env["HOME"]
    assert isinstance(home, str)
    assert home != str(auth_dir)
    assert env["NO_COLOR"] == "1"
    assert env["CODEX_HOME" if agent == "codex" else "CLAUDE_CONFIG_DIR"] == home


@pytest.mark.parametrize(
    ("agent", "returncode", "stdout"),
    (
        ("codex", 0, "Logged in using an API key\n"),
        ("codex", 1, "Not logged in\n"),
        (
            "claude",
            0,
            '{"loggedIn":true,"authMethod":"api_key",'
            '"apiProvider":"firstParty"}',
        ),
        (
            "claude",
            0,
            '{"loggedIn":true,"authMethod":"claude.ai",'
            '"apiProvider":"bedrock","subscriptionType":"pro"}',
        ),
        ("claude", 1, '{"loggedIn":false,"authMethod":"none"}'),
        ("claude", 0, "not-json"),
    ),
)
def test_probe_subscription_authentication_rejects_non_subscription_or_unknown_state(
    tmp_path: Path, agent: str, returncode: int, stdout: str
) -> None:
    executable = tmp_path / agent
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    auth_dir = tmp_path / f"{agent}-auth"
    auth_dir.mkdir()

    result = probe_subscription_authentication(
        agent,
        executable,
        auth_dir=auth_dir,
        run_cmd=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, returncode, stdout=stdout, stderr="credential detail must not escape"
        ),
    )

    assert result.compatible is False
    assert result.error == f"{agent} subscription authentication could not be verified"
    assert "credential detail" not in result.error


@pytest.mark.parametrize("subscription_type", ([], {}))
def test_probe_subscription_authentication_rejects_non_string_claude_subscription_type(
    tmp_path: Path, subscription_type: object
) -> None:
    executable = tmp_path / "claude"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    auth_dir = tmp_path / "claude-auth"
    auth_dir.mkdir()
    status = {
        "loggedIn": True,
        "authMethod": "claude.ai",
        "apiProvider": "firstParty",
        "subscriptionType": subscription_type,
    }

    result = probe_subscription_authentication(
        "claude",
        executable,
        auth_dir=auth_dir,
        run_cmd=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps(status), stderr=""
        ),
    )

    assert result.compatible is False
    assert result.error == "claude subscription authentication could not be verified"


def test_probe_subscription_authentication_supplies_claude_oauth_token_without_logging_it(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "claude"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    auth_dir = tmp_path / "claude-auth"
    auth_dir.mkdir()
    token = "synthetic-sensitive-oauth-token"
    token_file = auth_dir / "oauth-token"
    token_file.write_text(token, encoding="utf-8")
    token_file.chmod(0o600)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == token
        assert all(token not in arg for arg in argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=(
                '{"loggedIn":true,"authMethod":"oauth_token",'
                '"apiProvider":"firstParty"}'
            ),
            stderr="",
        )

    result = probe_subscription_authentication(
        "claude", executable, auth_dir=auth_dir, run_cmd=fake_run
    )

    assert result.compatible is True
    assert token not in result.details


@pytest.mark.parametrize(
    "settings",
    (
        '{"env":{"ANTHROPIC_API_KEY":"synthetic-secret"}}',
        '{"env":{"ANTHROPIC_BASE_URL":"https://gateway.invalid"}}',
        '{"apiKeyHelper":"/bin/false"}',
        "not-json",
    ),
)
def test_probe_subscription_authentication_rejects_claude_metered_settings(
    tmp_path: Path, settings: str
) -> None:
    executable = tmp_path / "claude"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    auth_dir = tmp_path / "claude-auth"
    auth_dir.mkdir()
    (auth_dir / "settings.json").write_text(settings, encoding="utf-8")

    result = probe_subscription_authentication(
        "claude",
        executable,
        auth_dir=auth_dir,
        run_cmd=lambda *args, **kwargs: pytest.fail(
            "auth status must not run with a metered or unknown settings file"
        ),
    )

    assert result.compatible is False
    assert result.error == "claude subscription authentication could not be verified"
    assert "synthetic-secret" not in result.error


@pytest.mark.parametrize("agent", ("claude", "codex"))
def test_probe_subscription_authentication_does_not_mutate_configured_auth_dir(
    tmp_path: Path, agent: str
) -> None:
    """A status CLI that writes into its HOME/config dir must not poison the operator's
    dedicated auth directory (e.g. Claude Code writing a `backups/` dir there breaks
    verify_native_isolation's 0700/0600 requirement for later native runs)."""
    executable = tmp_path / agent
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    auth_dir = tmp_path / f"{agent}-auth"
    auth_dir.mkdir(mode=0o700)
    before = sorted(p.relative_to(auth_dir) for p in auth_dir.rglob("*"))
    stdout = (
        ""
        if agent == "codex"
        else json.dumps(
            {
                "loggedIn": True,
                "authMethod": "oauth_token",
                "apiProvider": "firstParty",
            }
        )
    )
    stderr = "Logged in using ChatGPT" if agent == "codex" else ""

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        env = kwargs["env"]
        assert isinstance(env, dict)
        home = Path(env["HOME"])
        (home / "backups").mkdir(mode=0o755)
        (home / "written-by-cli").write_text("junk", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=stderr)

    result = probe_subscription_authentication(
        agent, executable, auth_dir=auth_dir, run_cmd=fake_run
    )

    assert result.compatible is True
    assert sorted(p.relative_to(auth_dir) for p in auth_dir.rglob("*")) == before
    assert stat.S_IMODE(auth_dir.stat().st_mode) == 0o700


def test_probe_command_capabilities_claude_success(tmp_path: Path) -> None:
    exe = tmp_path / "claude"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    version_out = (FIXTURES / "claude" / "cli-version.txt").read_text(encoding="utf-8")
    help_out = (FIXTURES / "claude" / "cli-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=version_out, stderr="")
        if "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=help_out, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

    res = probe_command_capabilities("claude", exe, run_cmd=fake_run)
    assert res.compatible is True
    assert res.version == "2.1.229"
    assert "headless flags verified" in res.details


def test_probe_command_capabilities_codex_success(tmp_path: Path) -> None:
    exe = tmp_path / "codex"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    version_out = (FIXTURES / "codex" / "cli-version.txt").read_text(encoding="utf-8")
    help_out = (FIXTURES / "codex" / "cli-exec-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=version_out, stderr="")
        if "exec" in argv and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=help_out, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

    res = probe_command_capabilities("codex", exe, run_cmd=fake_run)
    assert res.compatible is True
    assert res.version == "0.147.0"
    assert "headless flags verified" in res.details
    assert res.codex_approval_mode is CodexApprovalMode.ASK_FOR_APPROVAL_NEVER
    assert res.supports_effort_flag is False


def test_probe_command_capabilities_codex_detects_approve_for_me_drift(tmp_path: Path) -> None:
    """#291: Codex CLI 0.153.4 dropped `--ask-for-approval` from `codex exec --help`;
    doctor/preflight must still report compatible=True and record which approval-flag
    variant is safe to use, rather than fail-closed on an actively supported CLI."""
    exe = tmp_path / "codex"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    version_out = (FIXTURES / "codex" / "cli-version-0.153.4.txt").read_text(encoding="utf-8")
    help_out = (FIXTURES / "codex" / "cli-exec-help-0.153.4.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=version_out, stderr="")
        if "exec" in argv and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=help_out, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

    res = probe_command_capabilities("codex", exe, run_cmd=fake_run)
    assert res.compatible is True
    assert res.version == "0.153.4"
    assert res.codex_approval_mode is CodexApprovalMode.APPROVE_FOR_ME
    assert res.supports_effort_flag is True


def test_probe_command_capabilities_codex_fails_closed_when_approval_flag_unknown(
    tmp_path: Path,
) -> None:
    """A Codex CLI whose `exec --help` no longer lists either known approval flag is an
    unrecognized/ambiguous approval contract and must fail closed rather than dispatch."""
    exe = tmp_path / "codex"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    help_out = (
        "--strict-config --sandbox --ephemeral --ignore-user-config "
        "--ignore-rules --output-schema --json"
    )

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="codex-cli 0.200.0\n", stderr="")
        if "exec" in argv and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=help_out, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

    res = probe_command_capabilities("codex", exe, run_cmd=fake_run)
    assert res.compatible is False
    assert "required Codex CLI flags are missing" in (res.error or "")


def test_probe_command_capabilities_codex_fails_closed_when_exec_help_fails(
    tmp_path: Path,
) -> None:
    """Top-level help cannot prove which flags the `exec` subcommand accepts."""
    exe = tmp_path / "codex"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    top_level_help = (FIXTURES / "codex" / "cli-exec-help-0.153.4.txt").read_text(
        encoding="utf-8"
    )

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="codex-cli 0.153.4\n", stderr="")
        if "exec" in argv and "--help" in argv:
            return subprocess.CompletedProcess(argv, 2, stdout="", stderr="exec unavailable")
        if "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=top_level_help, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

    res = probe_command_capabilities("codex", exe, run_cmd=fake_run)

    assert res.compatible is False
    assert res.error == "codex inspection failed (version or exec help exited nonzero)"


def test_probe_command_capabilities_rejects_missing_required_flags(tmp_path: Path) -> None:
    exe = tmp_path / "claude"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="2.1.229\n", stderr="")
        if "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="--print only\n", stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    res = probe_command_capabilities("claude", exe, run_cmd=fake_run)
    assert res.compatible is False
    assert "required Claude CLI flags are missing" in (res.error or "")


def test_probe_command_capabilities_rejects_version_drift(tmp_path: Path) -> None:
    exe = tmp_path / "claude"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="unrecognized-output\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    res = probe_command_capabilities("claude", exe, run_cmd=fake_run)
    assert res.compatible is False
    assert "unrecognized Claude CLI version" in (res.error or "")


def test_validate_native_preflight_only_probes_enabled_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("subsched.preflight.native_isolation_failure", lambda: None)
    claude_exe = tmp_path / "claude"
    claude_exe.write_text("", encoding="utf-8")
    claude_exe.chmod(0o755)

    git_exe = tmp_path / "git"
    git_exe.write_text("", encoding="utf-8")
    git_exe.chmod(0o755)

    gh_exe = tmp_path / "gh"
    gh_exe.write_text("", encoding="utf-8")
    gh_exe.chmod(0o755)

    def resolver(cmd: str) -> Path | None:
        if cmd == "claude":
            return claude_exe
        if cmd == "git":
            return git_exe
        if cmd == "gh":
            return gh_exe
        return None

    claude_v = (FIXTURES / "claude" / "cli-version.txt").read_text(encoding="utf-8")
    claude_h = (FIXTURES / "claude" / "cli-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        if name == "git":
            return subprocess.CompletedProcess(argv, 0, stdout="git version 2.40.0\n", stderr="")
        if name == "gh":
            return subprocess.CompletedProcess(argv, 0, stdout="gh version 2.50.0\n", stderr="")
        if name == "claude" and "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_v, stderr="")
        if name == "claude" and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_h, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    report = validate_native_preflight(
        enabled_agents=("claude",),
        executable_resolver=resolver,
        run_cmd=fake_run,
    )
    assert report.passed is True
    assert report.get("claude") is not None
    assert report.get("claude").compatible is True
    # Codex was not enabled, so it should not be in report checks
    assert report.get("codex") is None


def test_validate_native_preflight_checks_configured_auth_for_each_enabled_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.config import NativeIsolationConfig

    monkeypatch.setattr("subsched.preflight.verify_native_isolation", lambda *a, **k: None)
    executables: dict[str, Path] = {}
    for name in ("git", "gh", "claude", "codex", "docker"):
        executable = tmp_path / name
        executable.write_text("", encoding="utf-8")
        executable.chmod(0o755)
        executables[name] = executable
    auth_dirs = {name: tmp_path / f"{name}-auth" for name in ("claude", "codex")}
    for auth_dir in auth_dirs.values():
        auth_dir.mkdir()

    claude_help = (FIXTURES / "claude" / "cli-help.txt").read_text(encoding="utf-8")
    codex_help = (FIXTURES / "codex" / "cli-exec-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        if name in {"git", "gh"}:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{name} version 1\n", stderr="")
        if "--version" in argv:
            version = "2.1.229\n" if name == "claude" else "codex-cli 0.147.0\n"
            return subprocess.CompletedProcess(argv, 0, stdout=version, stderr="")
        if name == "claude" and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_help, stderr="")
        if name == "codex" and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=codex_help, stderr="")
        if name == "claude" and argv[1:3] == ["auth", "status"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=(
                    '{"loggedIn":true,"authMethod":"claude.ai",'
                    '"apiProvider":"firstParty","subscriptionType":"max"}'
                ),
                stderr="",
            )
        if name == "codex" and argv[1:] == ["login", "status"]:
            return subprocess.CompletedProcess(
                argv, 0, stdout="", stderr="Logged in using ChatGPT\n"
            )
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="unexpected")

    report = validate_native_preflight(
        enabled_agents=("claude", "codex"),
        isolation_config=NativeIsolationConfig(
            backend="container",
            runtime="docker",
            image="worker@sha256:" + "a" * 64,
            network="internal",
            proxy_url="http://proxy:3128",
            proxy_image="proxy@sha256:" + "b" * 64,
            auth=tuple(auth_dirs.items()),
        ),
        executable_resolver=lambda name: executables.get(name),
        run_cmd=fake_run,
    )

    assert report.passed is True
    assert report.get("claude-auth").compatible is True
    assert report.get("codex-auth").compatible is True


def test_validate_native_preflight_fails_when_enabled_agent_auth_is_unverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.config import NativeIsolationConfig

    monkeypatch.setattr("subsched.preflight.verify_native_isolation", lambda *a, **k: None)
    executables: dict[str, Path] = {}
    for name in ("git", "gh", "codex", "docker"):
        executable = tmp_path / name
        executable.write_text("", encoding="utf-8")
        executable.chmod(0o755)
        executables[name] = executable
    auth_dir = tmp_path / "codex-auth"
    auth_dir.mkdir()
    codex_help = (FIXTURES / "codex" / "cli-exec-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        if name in {"git", "gh"}:
            return subprocess.CompletedProcess(argv, 0, stdout=f"{name} version 1\n", stderr="")
        if "--version" in argv:
            return subprocess.CompletedProcess(
                argv, 0, stdout="codex-cli 0.147.0\n", stderr=""
            )
        if "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=codex_help, stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout="Logged in using an API key\n", stderr=""
        )

    report = validate_native_preflight(
        enabled_agents=("codex",),
        isolation_config=NativeIsolationConfig(
            backend="container",
            runtime="docker",
            image="worker@sha256:" + "a" * 64,
            network="internal",
            proxy_url="http://proxy:3128",
            proxy_image="proxy@sha256:" + "b" * 64,
            auth=(("codex", auth_dir),),
        ),
        executable_resolver=lambda name: executables.get(name),
        run_cmd=fake_run,
    )

    assert report.passed is False
    assert report.get("codex-auth").compatible is False
    assert any("codex subscription authentication" in reason for reason in report.failure_reasons)


def test_validate_native_preflight_fails_closed_when_model_configured_but_unsupported(
    tmp_path: Path,
) -> None:
    """#296: agents.claude.models has a configured model, but the fixture --help output
    (an older CLI dump) does not advertise --model, so preflight must fail closed here --
    before any dispatch -- instead of silently omitting the flag or falling back."""
    from subsched.config import AgentModelPolicy, AgentSettings

    claude_exe = tmp_path / "claude"
    claude_exe.write_text("", encoding="utf-8")
    claude_exe.chmod(0o755)
    git_exe = tmp_path / "git"
    git_exe.write_text("", encoding="utf-8")
    git_exe.chmod(0o755)
    gh_exe = tmp_path / "gh"
    gh_exe.write_text("", encoding="utf-8")
    gh_exe.chmod(0o755)

    def resolver(cmd: str) -> Path | None:
        return {"claude": claude_exe, "git": git_exe, "gh": gh_exe}.get(cmd)

    claude_v = (FIXTURES / "claude" / "cli-version.txt").read_text(encoding="utf-8")
    claude_h = (FIXTURES / "claude" / "cli-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        if name == "git":
            return subprocess.CompletedProcess(argv, 0, stdout="git version 2.40.0\n", stderr="")
        if name == "gh":
            return subprocess.CompletedProcess(argv, 0, stdout="gh version 2.50.0\n", stderr="")
        if name == "claude" and "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_v, stderr="")
        if name == "claude" and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_h, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    report = validate_native_preflight(
        enabled_agents=("claude",),
        executable_resolver=resolver,
        run_cmd=fake_run,
        agents={"claude": AgentSettings(models=AgentModelPolicy(default="sonnet"))},
    )
    assert report.passed is False
    assert any("does not support a --model flag" in reason for reason in report.failure_reasons)
    # The CLI itself is still compatible for non-model dispatch; only the model policy
    # requirement fails closed.
    assert report.get("claude").compatible is True
    assert report.get("claude").supports_model_flag is False


def test_validate_native_preflight_fails_when_effort_configured_and_cli_lacks_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.config import AgentEffortPolicy, AgentSettings

    monkeypatch.setattr("subsched.preflight.native_isolation_failure", lambda: None)
    claude_exe = tmp_path / "claude"
    claude_exe.write_text("", encoding="utf-8")
    claude_exe.chmod(0o755)
    git_exe = tmp_path / "git"
    git_exe.write_text("", encoding="utf-8")
    git_exe.chmod(0o755)
    gh_exe = tmp_path / "gh"
    gh_exe.write_text("", encoding="utf-8")
    gh_exe.chmod(0o755)

    def resolver(cmd: str) -> Path | None:
        return {"claude": claude_exe, "git": git_exe, "gh": gh_exe}.get(cmd)

    claude_v = (FIXTURES / "claude" / "cli-version.txt").read_text(encoding="utf-8")
    # cli-help.txt does not advertise --effort
    claude_h = (FIXTURES / "claude" / "cli-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        if name == "git":
            return subprocess.CompletedProcess(argv, 0, stdout="git version 2.40.0\n", stderr="")
        if name == "gh":
            return subprocess.CompletedProcess(argv, 0, stdout="gh version 2.50.0\n", stderr="")
        if name == "claude" and "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_v, stderr="")
        if name == "claude" and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_h, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    report = validate_native_preflight(
        enabled_agents=("claude",),
        executable_resolver=resolver,
        run_cmd=fake_run,
        agents={"claude": AgentSettings(effort=AgentEffortPolicy(default="high"))},
    )
    assert report.passed is False
    assert any(
        "does not support reasoning effort flags" in reason
        for reason in report.failure_reasons
    )
    assert report.get("claude").compatible is True
    assert report.get("claude").supports_effort_flag is False


def test_validate_native_preflight_passes_when_effort_configured_and_cli_supports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.config import AgentEffortPolicy, AgentSettings

    monkeypatch.setattr("subsched.preflight.native_isolation_failure", lambda: None)
    claude_exe = tmp_path / "claude"
    claude_exe.write_text("", encoding="utf-8")
    claude_exe.chmod(0o755)
    git_exe = tmp_path / "git"
    git_exe.write_text("", encoding="utf-8")
    git_exe.chmod(0o755)
    gh_exe = tmp_path / "gh"
    gh_exe.write_text("", encoding="utf-8")
    gh_exe.chmod(0o755)

    def resolver(cmd: str) -> Path | None:
        return {"claude": claude_exe, "git": git_exe, "gh": gh_exe}.get(cmd)

    claude_v = (FIXTURES / "claude" / "cli-version.txt").read_text(encoding="utf-8")
    claude_h = (
        (FIXTURES / "claude" / "cli-help.txt").read_text(encoding="utf-8")
        + "\n  --effort <level>  Effort level\n"
    )

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        if name == "git":
            return subprocess.CompletedProcess(argv, 0, stdout="git version 2.40.0\n", stderr="")
        if name == "gh":
            return subprocess.CompletedProcess(argv, 0, stdout="gh version 2.50.0\n", stderr="")
        if name == "claude" and "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_v, stderr="")
        if name == "claude" and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_h, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    report = validate_native_preflight(
        enabled_agents=("claude",),
        executable_resolver=resolver,
        run_cmd=fake_run,
        agents={"claude": AgentSettings(effort=AgentEffortPolicy(default="high"))},
    )
    assert report.passed is True
    assert report.get("claude").supports_effort_flag is True


def test_validate_native_preflight_fails_when_codex_effort_configured_and_cli_lacks_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.config import AgentEffortPolicy, AgentSettings

    monkeypatch.setattr("subsched.preflight.native_isolation_failure", lambda: None)
    codex_exe = tmp_path / "codex"
    codex_exe.write_text("", encoding="utf-8")
    codex_exe.chmod(0o755)
    git_exe = tmp_path / "git"
    git_exe.write_text("", encoding="utf-8")
    git_exe.chmod(0o755)
    gh_exe = tmp_path / "gh"
    gh_exe.write_text("", encoding="utf-8")
    gh_exe.chmod(0o755)

    def resolver(cmd: str) -> Path | None:
        return {"codex": codex_exe, "git": git_exe, "gh": gh_exe}.get(cmd)

    codex_v = (FIXTURES / "codex" / "cli-version.txt").read_text(encoding="utf-8")
    # cli-exec-help.txt does not advertise -c or --config
    codex_h = (FIXTURES / "codex" / "cli-exec-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        if name == "git":
            return subprocess.CompletedProcess(argv, 0, stdout="git version 2.40.0\n", stderr="")
        if name == "gh":
            return subprocess.CompletedProcess(argv, 0, stdout="gh version 2.50.0\n", stderr="")
        if name == "codex" and "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=codex_v, stderr="")
        if name == "codex" and "exec" in argv and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=codex_h, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    report = validate_native_preflight(
        enabled_agents=("codex",),
        executable_resolver=resolver,
        run_cmd=fake_run,
        agents={"codex": AgentSettings(effort=AgentEffortPolicy(default="high"))},
    )
    assert report.passed is False
    assert any(
        "does not support reasoning effort flags" in reason
        for reason in report.failure_reasons
    )
    assert report.get("codex").compatible is True
    assert report.get("codex").supports_effort_flag is False


def test_validate_native_preflight_passes_when_codex_effort_configured_and_cli_supports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.config import AgentEffortPolicy, AgentSettings

    monkeypatch.setattr("subsched.preflight.native_isolation_failure", lambda: None)
    codex_exe = tmp_path / "codex"
    codex_exe.write_text("", encoding="utf-8")
    codex_exe.chmod(0o755)
    git_exe = tmp_path / "git"
    git_exe.write_text("", encoding="utf-8")
    git_exe.chmod(0o755)
    gh_exe = tmp_path / "gh"
    gh_exe.write_text("", encoding="utf-8")
    gh_exe.chmod(0o755)

    def resolver(cmd: str) -> Path | None:
        return {"codex": codex_exe, "git": git_exe, "gh": gh_exe}.get(cmd)

    codex_v = (FIXTURES / "codex" / "cli-version-0.153.4.txt").read_text(encoding="utf-8")
    # cli-exec-help-0.153.4.txt advertises -c, --config
    codex_h = (FIXTURES / "codex" / "cli-exec-help-0.153.4.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        if name == "git":
            return subprocess.CompletedProcess(argv, 0, stdout="git version 2.40.0\n", stderr="")
        if name == "gh":
            return subprocess.CompletedProcess(argv, 0, stdout="gh version 2.50.0\n", stderr="")
        if name == "codex" and "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=codex_v, stderr="")
        if name == "codex" and "exec" in argv and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=codex_h, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    report = validate_native_preflight(
        enabled_agents=("codex",),
        executable_resolver=resolver,
        run_cmd=fake_run,
        agents={"codex": AgentSettings(effort=AgentEffortPolicy(default="high"))},
    )
    assert report.passed is True
    assert report.get("codex").supports_effort_flag is True


def test_validate_native_preflight_passes_when_no_model_configured_regardless_of_cli_support(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No `agents=` mapping, or an agent with no model configured, must preserve exact
    pre-#296 behavior: no fail-closed check runs at all."""
    monkeypatch.setattr("subsched.preflight.native_isolation_failure", lambda: None)
    claude_exe = tmp_path / "claude"
    claude_exe.write_text("", encoding="utf-8")
    claude_exe.chmod(0o755)
    git_exe = tmp_path / "git"
    git_exe.write_text("", encoding="utf-8")
    git_exe.chmod(0o755)
    gh_exe = tmp_path / "gh"
    gh_exe.write_text("", encoding="utf-8")
    gh_exe.chmod(0o755)

    def resolver(cmd: str) -> Path | None:
        return {"claude": claude_exe, "git": git_exe, "gh": gh_exe}.get(cmd)

    claude_v = (FIXTURES / "claude" / "cli-version.txt").read_text(encoding="utf-8")
    claude_h = (FIXTURES / "claude" / "cli-help.txt").read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        name = Path(argv[0]).name
        if name == "git":
            return subprocess.CompletedProcess(argv, 0, stdout="git version 2.40.0\n", stderr="")
        if name == "gh":
            return subprocess.CompletedProcess(argv, 0, stdout="gh version 2.50.0\n", stderr="")
        if name == "claude" and "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_v, stderr="")
        if name == "claude" and "--help" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=claude_h, stderr="")
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    report = validate_native_preflight(
        enabled_agents=("claude",),
        executable_resolver=resolver,
        run_cmd=fake_run,
    )
    assert report.passed is True


def test_validate_native_preflight_probes_never_invoke_model_or_expose_credentials(
    tmp_path: Path,
) -> None:
    exe = tmp_path / "codex"
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)

    captured_runs: list[dict[str, object]] = []

    def recording_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured_runs.append({"argv": argv, "env": kwargs.get("env")})
        return subprocess.CompletedProcess(argv, 0, stdout="codex-cli 0.147.0\n", stderr="")

    probe_command_capabilities("codex", exe, run_cmd=recording_run)

    for run_info in captured_runs:
        argv = run_info["argv"]
        assert isinstance(argv, list)
        assert not any("sk-" in arg for arg in argv)
        # Probe commands must be --version or --help, never prompts or tasks
        assert any(flag in argv for flag in ("--version", "--help"))
        # Env should be empty to prevent credential leakage
        assert run_info["env"] == {}


def test_validate_native_preflight_fails_when_write_policy_requires_auth_and_gh_unauthenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_exe = tmp_path / "git"
    git_exe.write_text("", encoding="utf-8")
    git_exe.chmod(0o755)

    gh_exe = tmp_path / "gh"
    gh_exe.write_text("", encoding="utf-8")
    gh_exe.chmod(0o755)

    def resolver(cmd: str) -> Path | None:
        if cmd == "git":
            return git_exe
        if cmd == "gh":
            return gh_exe
        return None

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 0, stdout="version 1.0\n", stderr="")

    from subsched.github.issues import TokenDiagnosis
    monkeypatch.setattr(
        "subsched.preflight.diagnose_token",
        lambda: TokenDiagnosis(
            authenticated=False,
            scopes=(),
            can_discover=False,
            can_write=False,
            broad_scopes=(),
        ),
    )

    report = validate_native_preflight(
        enabled_agents=(),
        write_policy_requires_auth=True,
        executable_resolver=resolver,
        run_cmd=fake_run,
    )
    assert report.passed is False
    assert any("GitHub authentication required" in r for r in report.failure_reasons)


def test_doctor_and_run_share_capability_failure_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from subsched.cli import app

    runner = CliRunner()
    failing_report = PreflightReport(
        checks=(
            PreflightCheckResult("git", True, compatible=True),
            PreflightCheckResult("gh", True, compatible=True),
            PreflightCheckResult(
                "claude",
                True,
                compatible=False,
                error="required Claude CLI flags are missing",
            ),
        ),
        passed=False,
        failure_reasons=(
            "claude compatibility check failed: required Claude CLI flags are missing",
        ),
    )

    monkeypatch.setattr(
        "subsched.cli.validate_native_preflight",
        lambda *args, **kwargs: failing_report,
    )

    # doctor command check
    doctor_res = runner.invoke(app, ["doctor"])
    assert doctor_res.exit_code == 1
    assert "compatibility error" in doctor_res.output
    assert "required Claude CLI flags are missing" in doctor_res.output

    # run command check
    run_res = runner.invoke(
        app,
        [
            "run",
            "--repo",
            "owner/repo",
            "--issues",
            "1",
            "--allow-native",
            "--subscription-billing-verified",
        ],
    )
    assert run_res.exit_code == 2
    assert "Native execution pre-flight doctor check failed" in run_res.output
    assert "required Claude CLI flags are missing" in run_res.output
def test_run_passes_configured_agent_models_to_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#296: `run` must forward the loaded `agents.<provider>.models` config to
    validate_native_preflight() so an agent with a configured model, but an installed
    CLI that doesn't support --model, fails closed here rather than at dispatch."""
    from typer.testing import CliRunner

    from subsched.cli import app

    runner = CliRunner()
    captured: dict[str, object] = {}

    def fake_preflight(*, agents=None, **kwargs: object) -> PreflightReport:
        captured["agents"] = agents
        return PreflightReport(
            checks=(
                PreflightCheckResult("git", True, compatible=True),
                PreflightCheckResult("gh", True, compatible=True),
                PreflightCheckResult("claude", True, compatible=True),
            ),
            passed=True,
            failure_reasons=(),
        )

    monkeypatch.setattr("subsched.cli.validate_native_preflight", fake_preflight)

    config_file = tmp_path / "scheduler.yaml"
    config_file.write_text(
        "github:\n"
        "  repo: owner/repo\n"
        "agents:\n"
        "  claude:\n"
        "    enabled: true\n"
        "    models:\n"
        "      default: sonnet\n",
        encoding="utf-8",
    )

    # Discovery/dispatch will fail after preflight (no real GitHub token/tasks here);
    # only the preflight call itself is under test.
    runner.invoke(
        app,
        [
            "run",
            "--repo",
            "owner/repo",
            "--issues",
            "1",
            "--allow-native",
            "--subscription-billing-verified",
            "--config",
            str(config_file),
        ],
    )

    assert "agents" in captured
    assert captured["agents"] is not None
    assert captured["agents"]["claude"].models.default == "sonnet"


def test_extract_command_binary() -> None:
    from subsched.preflight import extract_command_binary

    assert extract_command_binary("cargo test") == "cargo"
    assert extract_command_binary("cargo fmt --check") == "cargo"
    assert extract_command_binary("pytest -v") == "pytest"
    assert extract_command_binary("uv run pytest") == "uv"
    assert extract_command_binary("RUST_BACKTRACE=1 cargo test") == "cargo"
    assert extract_command_binary("./scripts/quality_gate.sh") == "./scripts/quality_gate.sh"
    assert extract_command_binary("") is None


def test_probe_container_toolchain_success() -> None:
    from subsched.agents.isolation import probe_container_toolchain

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert "command -v cargo" in argv[-1]
        return subprocess.CompletedProcess(argv, 0, "/root/.cargo/bin/cargo\n", "")

    assert (
        probe_container_toolchain("docker", "my-image@sha256:abc", "cargo", run_cmd=fake_run)
        is True
    )


def test_probe_container_toolchain_missing() -> None:
    from subsched.agents.isolation import probe_container_toolchain

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "")

    assert (
        probe_container_toolchain("docker", "my-image@sha256:abc", "cargo", run_cmd=fake_run)
        is False
    )


def test_validate_native_preflight_fails_when_isolation_toolchain_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.config import NativeIsolationConfig

    config = NativeIsolationConfig(
        backend="container",
        runtime="/usr/bin/docker",
        image="ghcr.io/example/worker@sha256:1111111111111111111111111111111111111111111111111111111111111111",
        network="subsched-internal",
        proxy_url="http://subsched-proxy:3128",
        proxy_image="ghcr.io/example/proxy@sha256:2222222222222222222222222222222222222222222222222222222222222222",
        auth={},
    )
    monkeypatch.setattr("subsched.preflight.verify_native_isolation", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "subsched.preflight.probe_command_capabilities",
        lambda name, executable, **kwargs: PreflightCheckResult(
            name=name, found=True, executable_path=executable, compatible=True
        ),
    )

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "command -v cargo" in argv[-1]:
            return subprocess.CompletedProcess(argv, 1, "", "")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    report = validate_native_preflight(
        enabled_agents=(),
        isolation_config=config,
        executable_resolver=lambda name: Path(f"/usr/bin/{name}"),
        run_cmd=fake_run,
        verification_commands=("cargo fmt --check", "cargo test"),
    )

    assert report.passed is False
    check = report.get("isolation-toolchain")
    assert check is not None
    assert check.found is False
    assert check.compatible is False
    assert "cargo" in (check.error or "")
    assert "Pre-bake" in (check.error or "")


def test_validate_native_preflight_passes_when_isolation_toolchain_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from subsched.config import NativeIsolationConfig

    config = NativeIsolationConfig(
        backend="container",
        runtime="/usr/bin/docker",
        image="ghcr.io/example/worker@sha256:1111111111111111111111111111111111111111111111111111111111111111",
        network="subsched-internal",
        proxy_url="http://subsched-proxy:3128",
        proxy_image="ghcr.io/example/proxy@sha256:2222222222222222222222222222222222222222222222222222222222222222",
        auth={},
    )
    monkeypatch.setattr("subsched.preflight.verify_native_isolation", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "subsched.preflight.probe_command_capabilities",
        lambda name, executable, **kwargs: PreflightCheckResult(
            name=name, found=True, executable_path=executable, compatible=True
        ),
    )

    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "command -v cargo" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, "/usr/bin/cargo\n", "")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    report = validate_native_preflight(
        enabled_agents=(),
        isolation_config=config,
        executable_resolver=lambda name: Path(f"/usr/bin/{name}"),
        run_cmd=fake_run,
        verification_commands=("cargo fmt --check", "cargo test"),
    )

    assert report.passed is True
    check = report.get("isolation-toolchain")
    assert check is not None
    assert check.found is True
    assert check.compatible is True
    assert "cargo" in check.details
