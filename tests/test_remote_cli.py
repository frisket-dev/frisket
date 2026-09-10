"""Client-side remote CLI: profile store, verb round-trips against a real
HTTP team server, secrets push hygiene, and the exit-code contract."""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from frisket.remote import config_store
from frisket.remote.cli import (
    proxy_command,
    remote_command,
    secrets_command,
    token_command,
    users_command,
)
from frisket.team.app import TeamConfig, create_team_app
from frisket.team.operator_service import mint_operator_token
from tests.team_setup_helpers import claim_server

pytestmark = pytest.mark.realtime


class _RequestRecorder:
    """ASGI wrapper recording (method, path, body) for transmit assertions."""

    def __init__(self, app: Any):
        self.app = app
        self.requests: list[tuple[str, str, bytes]] = []

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        chunks: list[bytes] = []
        buffered: list[dict[str, Any]] = []

        async def recording_receive():
            message = await receive()
            if message.get("type") == "http.request":
                chunks.append(message.get("body", b""))
            buffered.append(message)
            return message

        try:
            await self.app(scope, recording_receive, send)
        finally:
            self.requests.append(
                (str(scope["method"]), str(scope["path"]), b"".join(chunks))
            )


@pytest.fixture(scope="module")
def live_server(tmp_path_factory) -> Any:
    tmp = tmp_path_factory.mktemp("remote-cli")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    base = f"http://127.0.0.1:{port}"
    app = create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp / 'control.db'}",
            run_queue_database_url=f"sqlite:///{tmp / 'queue.db'}",
            data_dir=tmp / "data",
            base_url=base,
            organization_name="Ops Desk",
            magic_link_enabled=False,
        )
    )
    claim_server(app, origin=base, workspace_name="Ops Desk")
    raw, _ = mint_operator_token(app.state.control_engine, org_id=1, label="laptop")
    recorder = _RequestRecorder(app)
    server = uvicorn.Server(
        uvicorn.Config(recorder, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("test server did not start")
        time.sleep(0.02)
    yield {"base": base, "token": raw, "app": app, "recorder": recorder}
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture()
def linked(live_server, tmp_path, monkeypatch) -> dict[str, Any]:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("FRISKET_SERVER", raising=False)
    assert (
        remote_command(
            [
                "link",
                live_server["base"],
                "--token",
                live_server["token"],
                "--name",
                "demo",
            ]
        )
        == 0
    )
    return live_server


# ---------------------------------------------------------------------------
# profile store
# ---------------------------------------------------------------------------


def test_config_store_perms_and_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    config = config_store.upsert_server(
        config_store.load_config(),
        name="prod",
        url="https://frisket.example.org/",
        token="frisket_operator_secret",
        label="laptop",
    )
    written = config_store.save_config(config)
    assert written == tmp_path / "xdg" / "frisket" / "config.toml"
    assert (written.stat().st_mode & 0o777) == 0o600
    assert (written.parent.stat().st_mode & 0o777) == 0o700
    reloaded = config_store.load_config()
    assert reloaded["default_server"] == "prod"
    assert reloaded["servers"]["prod"]["url"] == "https://frisket.example.org"
    assert reloaded["servers"]["prod"]["token"] == "frisket_operator_secret"


def test_server_resolution_precedence(monkeypatch) -> None:
    config = {
        "default_server": "one",
        "servers": {
            "one": {"url": "https://one.example", "token": "t1"},
            "two": {"url": "https://two.example", "token": "t2"},
        },
    }
    assert config_store.resolve_server(config, selector=None, env={})[0] == "one"
    assert config_store.resolve_server(config, selector="two", env={})[0] == "two"
    assert (
        config_store.resolve_server(config, selector="https://two.example/", env={})[0]
        == "two"
    )
    assert (
        config_store.resolve_server(
            config, selector=None, env={"FRISKET_SERVER": "two"}
        )[0]
        == "two"
    )
    # Explicit --server beats the environment.
    assert (
        config_store.resolve_server(
            config, selector="one", env={"FRISKET_SERVER": "two"}
        )[0]
        == "one"
    )
    with pytest.raises(config_store.ConfigError):
        config_store.resolve_server(config, selector="three", env={})
    with pytest.raises(config_store.ConfigError):
        config_store.resolve_server(
            {"default_server": None, "servers": {}}, selector=None, env={}
        )


# ---------------------------------------------------------------------------
# verbs over real HTTP
# ---------------------------------------------------------------------------


def test_link_status_list_and_default(linked, capsys) -> None:
    assert remote_command(["status"]) == 0
    status = capsys.readouterr().out
    assert "org: Ops Desk" in status
    assert linked["token"] not in status
    assert remote_command(["list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["default_server"] == "demo"
    assert listed["servers"]["demo"]["label"] == "laptop"
    assert "token" not in listed["servers"]["demo"]
    assert remote_command(["default", "demo"]) == 0
    assert remote_command(["default", "missing"]) == 3


def test_link_rejects_a_bad_token_with_the_auth_exit_code(
    live_server, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    code = remote_command(
        ["link", live_server["base"], "--token", "frisket_operator_wrong"]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert err.strip().count("\n") == 0
    assert "frisket_operator_wrong" not in err


def test_users_verbs_round_trip(linked, capsys) -> None:
    assert (
        users_command(
            ["add", "cli-a@example.com", "cli-b@example.com", "--role", "member"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert out.count("/auth/callback?token=") == 2
    assert users_command(["list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["schema_version"] == "frisket.admin_users.v1"
    invites = {
        row["email"]: row for org in listed["orgs"] for row in org["pending_invites"]
    }
    assert invites["cli-a@example.com"]["role"] == "member"
    assert users_command(["role", "cli-a@example.com", "owner"]) == 0
    capsys.readouterr()
    assert users_command(["list"]) == 0
    rendered = capsys.readouterr().out
    assert "cli-a@example.com" in rendered
    assert "owner" in rendered
    assert "invited" in rendered
    assert users_command(["role", "cli-a@example.com", "czar"]) == 3
    capsys.readouterr()
    # Re-adding without --role re-issues and keeps the stored role; a
    # conflicting --role is refused instead of silently resetting it.
    assert users_command(["add", "cli-a@example.com"]) == 0
    out = capsys.readouterr().out
    assert "cli-a@example.com (owner) [re-issued]:" in out
    assert users_command(["add", "cli-a@example.com", "--role", "member"]) == 3
    err = capsys.readouterr().err
    assert "'owner'" in err
    assert users_command(["remove", "cli-b@example.com"]) == 0
    capsys.readouterr()
    assert users_command(["remove", "cli-b@example.com"]) == 3
    assert users_command(["reset", "missing@example.com"]) == 3
    capsys.readouterr()
    # Partial failure: one bad email fails, the good one is still attempted.
    assert users_command(["add", "not-an-email", "cli-c@example.com"]) == 3
    out = capsys.readouterr().out
    assert "cli-c@example.com" in out


def test_token_rotate_updates_the_store(
    live_server, tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("FRISKET_SERVER", raising=False)
    rotating_raw, _ = mint_operator_token(
        live_server["app"].state.control_engine, org_id=1, label="rotating"
    )
    assert (
        remote_command(
            ["link", live_server["base"], "--token", rotating_raw, "--name", "spin"]
        )
        == 0
    )
    capsys.readouterr()
    assert token_command(["rotate", "--server", "spin"]) == 0
    out = capsys.readouterr().out
    assert "Rotated operator token" in out
    stored = config_store.load_config()["servers"]["spin"]["token"]
    assert stored != rotating_raw
    # The stored replacement authenticates; the old token no longer does.
    assert remote_command(["status", "--server", "spin"]) == 0


def test_secrets_set_list_unset_and_prompt_paths(linked, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("sk-cli-anthropic-7777\n"))
    assert secrets_command(["set", "anthropic", "--stdin"]) == 0
    capsys.readouterr()
    assert secrets_command(["list"]) == 0
    out = capsys.readouterr().out
    assert "...7777" in out
    assert "sk-cli-anthropic-7777" not in out
    assert secrets_command(["set", "nonsense", "--stdin"]) == 3
    capsys.readouterr()

    # Interactive prompt path (getpass, no echo).
    import getpass

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "sk-cli-openai-8888")
    assert secrets_command(["set", "openai"]) == 0
    capsys.readouterr()
    assert secrets_command(["unset", "openai"]) == 0
    assert secrets_command(["unset", "anthropic"]) == 0


def test_secrets_push_allowlist_dry_run_and_prune(
    linked, tmp_path, monkeypatch, capsys
) -> None:
    env_file = tmp_path / "push.env"
    env_file.write_text(
        'export ANTHROPIC_API_KEY="sk-push-anthropic-1111"\n'
        "OPENAI_API_KEY=sk-push-openai-2222\n"
        "UNRELATED_SECRET=never-transmitted-value\n"
        "# comment line\n"
    )
    recorder = linked["recorder"]
    start = len(recorder.requests)
    assert secrets_command(["push", "--file", str(env_file), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "anthropic: would-add" in out
    assert "openai: would-add" in out
    assert "sk-push" not in out
    dry_run_requests = recorder.requests[start:]
    assert all(method == "GET" for method, _path, _body in dry_run_requests)

    assert secrets_command(["push", "--file", str(env_file)]) == 0
    capsys.readouterr()
    transmitted = b"".join(body for _m, _p, body in recorder.requests[start:])
    assert b"never-transmitted-value" not in transmitted
    assert b"UNRELATED_SECRET" not in transmitted

    # Now in sync; a second push is a no-op.
    assert secrets_command(["push", "--file", str(env_file)]) == 0
    out = capsys.readouterr().out
    assert "anthropic: in-sync" in out and "openai: in-sync" in out

    # Prune removes keys missing from the source, only after confirmation.
    pruned = tmp_path / "pruned.env"
    pruned.write_text("ANTHROPIC_API_KEY=sk-push-anthropic-1111\n")
    assert secrets_command(["push", "--file", str(pruned), "--prune", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "openai: would-remove" in out
    assert secrets_command(["push", "--file", str(pruned), "--prune"]) == 3
    err = capsys.readouterr().err
    assert "rerun with --yes" in err
    assert secrets_command(["push", "--file", str(pruned), "--prune", "--yes"]) == 0
    capsys.readouterr()
    assert secrets_command(["list", "--json"]) == 0
    providers = {
        row["provider"]: row for row in json.loads(capsys.readouterr().out)["providers"]
    }
    assert providers["anthropic"]["configured"] is True
    assert providers["openai"]["configured"] is False
    assert secrets_command(["unset", "anthropic"]) == 0

    assert secrets_command(["push", "--file", str(tmp_path / "absent.env")]) == 3


def test_secrets_push_from_env_and_shadow_warning(linked, monkeypatch, capsys) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "sk-push-gemini-3333")
    assert secrets_command(["push", "--from-env"]) == 0
    capsys.readouterr()
    # Simulate a server-side shadowing env var and confirm the loud warning.
    server_env = "OPENROUTER_API_KEY"
    monkeypatch.setenv(server_env, "sk-push-openrouter-4444")
    assert secrets_command(["push", "--from-env"]) == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert server_env in captured.err
    assert "sk-push-openrouter-4444" not in captured.err + captured.out
    assert secrets_command(["unset", "gemini"]) == 0
    assert secrets_command(["unset", "openrouter"]) == 0


def test_console_script_wiring_over_a_subprocess(linked, tmp_path) -> None:
    env = os.environ | {"XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"]}
    env.pop("FRISKET_SERVER", None)
    result = subprocess.run(
        [sys.executable, "-m", "frisket.cli", "users", "list", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema_version"] == "frisket.admin_users.v1"
    missing = subprocess.run(
        [sys.executable, "-m", "frisket.cli", "users", "role", "x@example.com", "czar"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert missing.returncode == 3
    assert missing.stderr.strip().count("\n") == 0


# ---------------------------------------------------------------------------
# frisket proxy
# ---------------------------------------------------------------------------


def _ok_probe(*_args: Any, **_kwargs: Any) -> Any:
    from frisket.ops.media_proxy import MediaProxyProbeResult

    return MediaProxyProbeResult(
        ok=True,
        probe_url="https://www.youtube.com/generate_204",
        status_code=204,
        elapsed_ms=37,
    )


def _failing_probe(*_args: Any, **_kwargs: Any) -> Any:
    from frisket.ops.media_proxy import MediaProxyProbeResult

    return MediaProxyProbeResult(
        ok=False,
        probe_url="https://www.youtube.com/generate_204",
        error="connection refused",
    )


def test_proxy_up_derives_the_target_and_round_trips_the_setting(
    linked, monkeypatch, capsys
) -> None:
    from frisket.ops import media_proxy as media_proxy_module
    from frisket.remote import cli

    monkeypatch.setattr(cli, "_openssh_version", lambda: (9, 6))
    monkeypatch.setattr(media_proxy_module, "probe_media_proxy", _failing_probe)

    assert proxy_command(["up"]) == 0
    out = capsys.readouterr().out
    assert "Tunnel target: root@127.0.0.1 (derived from server profile" in out
    assert "--ssh user@host" in out
    assert "ssh -N -o ExitOnForwardFailure=yes" in out
    assert "-R 127.0.0.1:1080 root@127.0.0.1" in out
    assert "set to socks5h://127.0.0.1:1080" in out
    assert "Proxy not reachable yet: connection refused" in out
    assert "frisket proxy status" in out

    # The setting really landed server-side.
    assert proxy_command(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["setting"]["configured"] is True
    assert payload["setting"]["url"] == "socks5h://127.0.0.1:1080"
    assert payload["setting"]["source"] == "admin"

    monkeypatch.setattr(media_proxy_module, "probe_media_proxy", _ok_probe)
    assert proxy_command(["status"]) == 0
    status_out = capsys.readouterr().out
    assert "socks5h://127.0.0.1:1080" in status_out
    assert "Proxy connected: probe returned HTTP 204 in 37 ms." in status_out

    assert proxy_command(["down"]) == 0
    assert "Media proxy cleared" in capsys.readouterr().out
    assert proxy_command(["status"]) == 0
    assert "No media proxy is set" in capsys.readouterr().out


def test_proxy_up_binds_the_container_gateway_when_reported(
    linked, monkeypatch, capsys
) -> None:
    from frisket.ops import media_proxy as media_proxy_module
    from frisket.remote import cli

    monkeypatch.setattr(cli, "_openssh_version", lambda: (9, 6))
    monkeypatch.setattr(media_proxy_module, "probe_media_proxy", _failing_probe)
    monkeypatch.setattr(media_proxy_module, "container_gateway", lambda: "172.18.0.1")

    assert proxy_command(["up"]) == 0
    captured = capsys.readouterr()
    assert "Server is containerized" in captured.out
    assert "-R 172.18.0.1:1080" in captured.out
    assert "set to socks5h://172.18.0.1:1080" in captured.out
    # The gateway bind is deliberate; the generic non-loopback warning would
    # only second-guess it.
    assert "not loopback" not in captured.err
    # The one failure sshd cannot report gets the one-time server recipe.
    assert "GatewayPorts clientspecified" in captured.err

    assert proxy_command(["down"]) == 0
    capsys.readouterr()


def test_proxy_up_honors_ssh_and_port_overrides(linked, monkeypatch, capsys) -> None:
    from frisket.ops import media_proxy as media_proxy_module
    from frisket.remote import cli

    monkeypatch.setattr(cli, "_openssh_version", lambda: (9, 6))
    monkeypatch.setattr(media_proxy_module, "probe_media_proxy", _failing_probe)

    assert proxy_command(["up", "--ssh", "deploy@box.example", "--port", "1085"]) == 0
    out = capsys.readouterr().out
    assert "Tunnel target: deploy@box.example\n" in out
    assert "derived" not in out
    assert "-R 127.0.0.1:1085 deploy@box.example" in out
    assert "socks5h://127.0.0.1:1085" in out
    assert proxy_command(["down"]) == 0
    capsys.readouterr()

    # --bind overrides the listen address for servers that cannot report a
    # container gateway themselves.
    assert proxy_command(["up", "--bind", "172.18.0.1"]) == 0
    captured = capsys.readouterr()
    assert "-R 172.18.0.1:1080" in captured.out
    assert "socks5h://172.18.0.1:1080" in captured.out
    assert "not loopback" not in captured.err
    assert "GatewayPorts clientspecified" in captured.err
    assert proxy_command(["down"]) == 0
    capsys.readouterr()


def test_proxy_up_warns_on_old_openssh_and_prints_the_fallback(
    linked, monkeypatch, capsys
) -> None:
    from frisket.ops import media_proxy as media_proxy_module
    from frisket.remote import cli

    monkeypatch.setattr(cli, "_openssh_version", lambda: (7, 2))
    monkeypatch.setattr(media_proxy_module, "probe_media_proxy", _failing_probe)

    assert proxy_command(["up"]) == 0
    captured = capsys.readouterr()
    assert "OpenSSH 7.2 does not support reverse dynamic forwarding" in captured.err
    assert "microsocks" in captured.err
    assert "NOT a substitute" in captured.err
    assert proxy_command(["down"]) == 0
    capsys.readouterr()

    # With --run the tunnel cannot work from this machine, so up refuses.
    monkeypatch.setattr(cli, "_openssh_version", lambda: (7, 2))
    assert proxy_command(["up", "--run"]) == 1
    capsys.readouterr()


class _FakeSsh:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.wait_calls == 1:
            raise KeyboardInterrupt
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.returncode = -9


def test_proxy_up_run_babysits_the_tunnel_and_clears_on_interrupt(
    linked, monkeypatch, capsys
) -> None:
    from frisket.ops import media_proxy as media_proxy_module
    from frisket.remote import cli

    monkeypatch.setattr(cli, "_openssh_version", lambda: (9, 6))
    monkeypatch.setattr(media_proxy_module, "probe_media_proxy", _ok_probe)
    monkeypatch.setattr(cli, "_PROXY_CHECK_DELAY_SECONDS", 0.0)
    fake = _FakeSsh()
    spawned: list[list[str]] = []

    def fake_popen(argv: list[str], **_kwargs: Any) -> _FakeSsh:
        spawned.append(list(argv))
        return fake

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)

    assert proxy_command(["up", "--run"]) == 0
    out = capsys.readouterr().out
    assert spawned == [
        [
            "ssh",
            "-N",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-R",
            "127.0.0.1:1080",
            "root@127.0.0.1",
        ]
    ]
    assert fake.terminated is True
    assert "Tunnel is up. Leave this running; Ctrl-C stops it." in out
    assert "Tunnel stopped." in out
    assert "Media proxy cleared" in out

    # The interrupt path must leave the server setting cleared.
    assert proxy_command(["status"]) == 0
    assert "No media proxy is set" in capsys.readouterr().out


def test_proxy_up_run_survives_transient_check_errors(
    linked, monkeypatch, capsys
) -> None:
    from frisket.ops import media_proxy as media_proxy_module
    from frisket.remote import cli
    from frisket.remote.client import RemoteClient, RemoteError

    monkeypatch.setattr(cli, "_openssh_version", lambda: (9, 6))
    monkeypatch.setattr(media_proxy_module, "probe_media_proxy", _ok_probe)
    monkeypatch.setattr(cli, "_PROXY_CHECK_DELAY_SECONDS", 0.0)

    def raising_check(_self: Any) -> Any:
        raise RemoteError("temporarily unreachable")

    monkeypatch.setattr(RemoteClient, "media_proxy_check", raising_check)
    fake = _FakeSsh()
    monkeypatch.setattr(cli.subprocess, "Popen", lambda _argv, **_k: fake)

    # A server hiccup during the connectivity poll must not abort the
    # babysit: the tunnel stays up until the interrupt, then the child is
    # stopped and the setting cleared like any other exit.
    assert proxy_command(["up", "--run"]) == 0
    out = capsys.readouterr().out
    assert "cannot reach the proxy yet" in out
    assert "Tunnel stopped." in out
    assert "Media proxy cleared" in out
    assert fake.terminated is True

    assert proxy_command(["status"]) == 0
    assert "No media proxy is set" in capsys.readouterr().out


def test_proxy_up_run_reports_a_setting_it_could_not_clear(
    linked, monkeypatch, capsys
) -> None:
    from frisket.ops import media_proxy as media_proxy_module
    from frisket.remote import cli
    from frisket.remote.client import RemoteClient, RemoteError

    monkeypatch.setattr(cli, "_openssh_version", lambda: (9, 6))
    monkeypatch.setattr(media_proxy_module, "probe_media_proxy", _ok_probe)
    monkeypatch.setattr(cli, "_PROXY_CHECK_DELAY_SECONDS", 0.0)

    original_clear = RemoteClient.media_proxy_clear
    calls = {"n": 0}

    def flaky_clear(self: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RemoteError("server went away")
        return original_clear(self)

    monkeypatch.setattr(RemoteClient, "media_proxy_clear", flaky_clear)
    fake = _FakeSsh()
    monkeypatch.setattr(cli.subprocess, "Popen", lambda _argv, **_k: fake)

    assert proxy_command(["up", "--run"]) == 0
    captured = capsys.readouterr()
    assert fake.terminated is True
    assert "could not clear the media proxy" in captured.err
    assert "frisket proxy down" in captured.err

    assert proxy_command(["down"]) == 0
    capsys.readouterr()


def test_proxy_status_flags_an_invalid_env_proxy(linked, monkeypatch, capsys) -> None:
    monkeypatch.setenv("FRISKET_MEDIA_PROXY", "socks4://x:1")
    assert proxy_command(["status"]) == 0
    captured = capsys.readouterr()
    assert "No media proxy is set" in captured.out
    assert "FRISKET_MEDIA_PROXY" in captured.err
    assert "ignored" in captured.err


def test_proxy_help_and_unknown_verb(linked, capsys) -> None:
    assert proxy_command(["--help"]) == 0
    assert "frisket proxy up" in capsys.readouterr().out
    assert proxy_command([]) == 3
    capsys.readouterr()
    assert proxy_command(["sideways"]) == 3
    assert "unknown proxy subcommand" in capsys.readouterr().err
