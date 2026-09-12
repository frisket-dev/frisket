"""Sandbox + broker tests. The property under test: op code can complete model
calls through the broker without a provider key ever entering its ENVIRONMENT
(`_scrubbed_env`/`SCRUB_PREFIXES`), and the CPython audit netwall refuses the
stdlib socket/subprocess surfaces.

Read that scope literally. This file covers the Python-level audit hook only —
the layer that raises before a syscall is ever attempted. It is not the
perimeter: on Linux a kernel fence (seccomp + Landlock, installed inside the
recipe child) is what actually refuses egress and out-of-scratch reads, and on
macOS and Windows there is no filesystem confinement at all. The real
perimeter, per platform, is pinned in test_sandbox_recipe_fence.py."""

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

import pytest

from frisket.ai.llm import (
    LLMRequest,
    LLMResponse,
    ModelRouter,
    ResponseCache,
    request_key,
)
from frisket.engine.sandbox.broker import CLIENT_SNIPPET, KeyBroker
from frisket.engine.sandbox.shim import (
    SandboxPolicy,
    run_python_op,
    run_sandboxed,
    run_sandboxed_stdout_lines,
)
from frisket.runtime.launch import worker_argv


@pytest.fixture
def sock_dir():
    """AF_UNIX paths cap at ~104 chars on macOS; pytest tmp_path is too deep.
    Windows has no /tmp and this fixture also backs the Windows loopback-TCP
    broker tests (which have no such path-length constraint), so it falls
    back to tempfile.gettempdir() there."""
    base_dir = tempfile.gettempdir() if os.name == "nt" else "/tmp"
    with tempfile.TemporaryDirectory(dir=base_dir, prefix="fk-") as d:
        yield Path(d)


_TRUSTED_NETWALL_SOCKET_EVENTS = {
    "socket.bind",
    "socket.connect",
    "socket.getaddrinfo",
    "socket.gethostbyaddr",
    "socket.gethostbyname",
    "socket.getnameinfo",
    "socket.sendmsg",
    "socket.sendto",
}

_TRUSTED_NETWALL_PROBE = r"""
import json
import os
import socket
import subprocess
import sys
from urllib.parse import unquote, urlsplit

denied = {}

def observe(name, operation):
    try:
        operation()
    except Exception as exc:
        denied[name] = type(exc).__name__
    else:
        denied[name] = None

udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
observe("socket.bind", lambda: socket.socket().bind(("127.0.0.1", 0)))
observe("socket.connect", lambda: socket.socket().connect(("127.0.0.1", 9)))
observe("socket.getaddrinfo", lambda: socket.getaddrinfo("localhost", 80))
observe("socket.gethostbyaddr", lambda: socket.gethostbyaddr("127.0.0.1"))
observe("socket.gethostbyname", lambda: socket.gethostbyname("localhost"))
observe(
    "socket.getnameinfo",
    lambda: socket.getnameinfo(("127.0.0.1", 80), 0),
)
observe("socket.sendto", lambda: udp.sendto(b"x", ("127.0.0.1", 9)))
observe(
    "socket.sendmsg",
    lambda: udp.sendmsg([b"x"], [], 0, ("127.0.0.1", 9)),
)
observe(
    "subprocess.Popen",
    lambda: subprocess.run([sys.executable, "-c", "pass"], check=False),
)

_broker_parts = urlsplit(os.environ["FRISKET_BROKER_ENDPOINT"])
broker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
broker.connect(unquote(_broker_parts.path))
broker.sendall(b"broker-ok")
broker.close()
print(json.dumps({"denied": denied, "broker_connected": True}, sort_keys=True))
"""


def _managed_recipe(source: str) -> tuple[list[str], bytes]:
    return worker_argv("recipe"), json.dumps({"source": source, "payload": {}}).encode()


def _force_trusted_linux_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("frisket.engine.sandbox.shim.sys.platform", "linux")
    monkeypatch.setattr(
        "frisket.engine.sandbox.shim._darwin_netwall_argv",
        lambda argv, scratch, policy, env: argv,
    )


def _broker_listener(path: Path) -> socket.socket:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.settimeout(2)
    listener.bind(str(path))
    listener.listen(1)
    return listener


def _assert_trusted_netwall_result(
    result, *, stdout: str, listener: socket.socket
) -> None:
    assert result.ok, result.stderr
    payload = json.loads(stdout)
    assert payload["broker_connected"] is True
    assert set(payload["denied"]) == _TRUSTED_NETWALL_SOCKET_EVENTS | {
        "subprocess.Popen"
    }
    assert set(payload["denied"].values()) == {"PermissionError"}
    connection, _ = listener.accept()
    with connection:
        assert connection.recv(32) == b"broker-ok"


class TestEnvScrubbing:
    def test_no_provider_keys_visible(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecret")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-alsosecret")
        monkeypatch.setenv("STRIPE_API_KEY", "rk_test_money")

        async def run():
            return await run_python_op(
                "import os, json, sys; print(json.dumps({'env': dict(os.environ)}))",
                {},
            )

        out = asyncio.run(run())
        env_str = json.dumps(out["env"])
        assert "supersecret" not in env_str
        assert "alsosecret" not in env_str
        assert "rk_test_money" not in env_str
        assert not any("API_KEY" in k for k in out["env"])

    def test_payload_arrives_on_stdin(self):
        async def run():
            return await run_python_op(
                "import sys, json; d=json.load(sys.stdin); "
                "print(json.dumps({'doubled': d['x'] * 2}))",
                {"x": 21},
            )

        assert asyncio.run(run())["doubled"] == 42

    def test_child_home_is_private_below_scratch(self, tmp_path, monkeypatch):
        ambient_home = tmp_path / "ambient-home"
        monkeypatch.setenv("HOME", str(ambient_home))
        scratch = tmp_path / "scratch"
        code = (
            "import json, os; from pathlib import Path; "
            "home=Path(os.environ['HOME']); "
            "print(json.dumps({'home': str(home), 'exists': home.is_dir()}))"
        )
        result = asyncio.run(
            run_sandboxed(
                [sys.executable, "-c", code],
                scratch_dir=scratch,
            )
        )
        payload = json.loads(result.stdout)
        child_home = Path(payload["home"])
        assert result.ok and payload["exists"] is True
        assert child_home.parent == scratch
        assert child_home != ambient_home

    def test_streaming_runner_uses_child_owned_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "ambient-home"))
        scratch = tmp_path / "stream-scratch"
        lines: list[str] = []
        result = asyncio.run(
            run_sandboxed_stdout_lines(
                [
                    sys.executable,
                    "-c",
                    "import os; from pathlib import Path; print(Path(os.environ['HOME']).parent)",
                ],
                on_stdout_line=lines.append,
                scratch_dir=scratch,
            )
        )
        assert result.ok
        assert lines == [str(scratch)]

    def test_trusted_python_netwall_blocks_buffered_socket_surface(
        self, monkeypatch, sock_dir
    ):
        _force_trusted_linux_path(monkeypatch)
        listener = _broker_listener(sock_dir / "buffered.sock")
        with listener:
            argv, request = _managed_recipe(_TRUSTED_NETWALL_PROBE)
            result = asyncio.run(
                run_sandboxed(
                    argv,
                    policy=SandboxPolicy(trusted_python_netwall=True),
                    stdin_data=request,
                    extra_env={
                        "FRISKET_BROKER_ENDPOINT": "unix://"
                        + quote(str(sock_dir / "buffered.sock"), safe="/")
                        + "?token=test-netwall-token"
                    },
                )
            )
            _assert_trusted_netwall_result(
                result, stdout=result.stdout, listener=listener
            )

    def test_trusted_python_netwall_blocks_streaming_socket_surface(
        self, monkeypatch, sock_dir
    ):
        _force_trusted_linux_path(monkeypatch)
        listener = _broker_listener(sock_dir / "streaming.sock")
        lines: list[str] = []
        with listener:
            argv, request = _managed_recipe(_TRUSTED_NETWALL_PROBE)
            result = asyncio.run(
                run_sandboxed_stdout_lines(
                    argv,
                    on_stdout_line=lines.append,
                    policy=SandboxPolicy(trusted_python_netwall=True),
                    stdin_data=request,
                    extra_env={
                        "FRISKET_BROKER_ENDPOINT": "unix://"
                        + quote(str(sock_dir / "streaming.sock"), safe="/")
                        + "?token=test-netwall-token"
                    },
                )
            )
            assert len(lines) == 1
            _assert_trusted_netwall_result(result, stdout=lines[0], listener=listener)

    def test_trusted_python_netwall_denies_non_loopback_tcp_broker_endpoint(
        self, monkeypatch
    ):
        """A tcp:// FRISKET_BROKER_ENDPOINT naming any host other than the
        literal 127.0.0.1 (e.g. a numeric non-loopback address) must never
        be admitted through the netwall allowlist: only the exact
        ("127.0.0.1", port) address is trusted. Before this fix, the
        allowlist trusted whatever hostname the endpoint URL carried, so
        tcp://1.1.1.1:443?token=x let the sandboxed process reach a real
        non-loopback address without DNS."""
        _force_trusted_linux_path(monkeypatch)
        probe = (
            "import json, socket\n"
            "out = {}\n"
            "try:\n"
            "    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "    s.settimeout(1)\n"
            "    s.connect(('1.1.1.1', 443))\n"
            "    out['connected'] = True\n"
            "except PermissionError as e:\n"
            "    out['connected'] = False\n"
            "    out['error'] = str(e)\n"
            "print(json.dumps(out))\n"
        )
        argv, request = _managed_recipe(probe)
        result = asyncio.run(
            run_sandboxed(
                argv,
                policy=SandboxPolicy(trusted_python_netwall=True),
                stdin_data=request,
                extra_env={"FRISKET_BROKER_ENDPOINT": "tcp://1.1.1.1:443?token=x"},
            )
        )
        assert result.ok, result.stderr
        payload = json.loads(result.stdout)
        assert payload["connected"] is False
        assert payload["error"] == "network access is disabled by the Frisket sandbox"

    def test_env_passthrough_cannot_override_child_owned_home(
        self, tmp_path, monkeypatch
    ):
        """A SandboxPolicy caller that passes HOME/USERPROFILE/TEMP/TMP
        through env_passthrough must not leak the ambient home into the
        child process -- the child-owned scratch home is a security/
        isolation invariant, not a caller-overridable default. Before this
        fix, `_scrubbed_env` set these four once up front but a later
        env_passthrough/extra_env merge could replace them when a caller passed
        HOME through."""
        ambient_home = tmp_path / "ambient-home"
        ambient_home.mkdir()
        monkeypatch.setenv("HOME", str(ambient_home))
        monkeypatch.setenv("USERPROFILE", str(ambient_home))
        monkeypatch.setenv("TEMP", str(ambient_home))
        monkeypatch.setenv("TMP", str(ambient_home))
        scratch = tmp_path / "scratch"
        code = (
            "import json, os; "
            "print(json.dumps({k: os.environ.get(k) for k in "
            "('HOME', 'USERPROFILE', 'TEMP', 'TMP')}))"
        )
        result = asyncio.run(
            run_sandboxed(
                [sys.executable, "-c", code],
                policy=SandboxPolicy(
                    env_passthrough=["HOME", "USERPROFILE", "TEMP", "TMP"]
                ),
                scratch_dir=scratch,
            )
        )
        assert result.ok, result.stderr
        payload = json.loads(result.stdout)
        for key, value in payload.items():
            child_path = Path(value)
            assert child_path.parent == scratch, (key, value)
            assert child_path != ambient_home, (key, value)

    def test_scrubbed_env_extra_cannot_override_child_owned_home(self):
        """Same invariant as above, exercised directly against
        `_scrubbed_env`: an `allowed_extra_env`-listed HOME override in the
        `extra` dict (the shape an internal caller like `run_python_op`
        passes) must not survive either."""
        from frisket.engine.sandbox.shim import _scrubbed_env

        env = _scrubbed_env(
            SandboxPolicy(allowed_extra_env=["HOME"]),
            {"HOME": "/attacker/home"},
            default_home=Path("/child/scratch-home"),
        )
        assert env["HOME"] == "/child/scratch-home"

    def test_scrubbed_env_pins_path_and_lang_unless_explicitly_passed_through(
        self, tmp_path, monkeypatch
    ):
        marker = tmp_path / "ambient-bin"
        marker.mkdir()
        ambient_path = str(marker)
        monkeypatch.setenv("PATH", ambient_path)
        monkeypatch.setenv("LANG", "ambient-locale")
        probe = (
            "import json, os; "
            "print(json.dumps({'PATH': os.environ['PATH'], "
            "'LANG': os.environ['LANG']}))"
        )

        pinned = asyncio.run(
            run_sandboxed(
                [sys.executable, "-c", probe],
                policy=SandboxPolicy(),
            )
        )
        assert pinned.ok, pinned.stderr
        assert json.loads(pinned.stdout) == {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
        }

        opted_in = asyncio.run(
            run_sandboxed(
                [sys.executable, "-c", probe],
                policy=SandboxPolicy(env_passthrough=["PATH"]),
            )
        )
        assert opted_in.ok, opted_in.stderr
        assert json.loads(opted_in.stdout) == {
            "PATH": ambient_path,
            "LANG": "C.UTF-8",
        }

    def test_trusted_python_netwall_rejects_other_commands(self, monkeypatch):
        with pytest.raises(ValueError, match="managed Python entrypoint"):
            asyncio.run(
                run_sandboxed(
                    [sys.executable, "-V"],
                    policy=SandboxPolicy(trusted_python_netwall=True),
                )
            )
        with pytest.raises(ValueError, match="managed Python entrypoint"):
            asyncio.run(
                run_sandboxed_stdout_lines(
                    [sys.executable, "-V"],
                    on_stdout_line=lambda line: None,
                    policy=SandboxPolicy(trusted_python_netwall=True),
                )
            )
        monkeypatch.setattr(
            "frisket.engine.sandbox.shim._has_process_netwall", lambda policy: False
        )
        monkeypatch.setattr("frisket.engine.sandbox.shim.sys.platform", "darwin")
        with pytest.raises(RuntimeError, match="sandbox-exec"):
            asyncio.run(
                run_sandboxed(
                    [sys.executable, "-c", "pass"],
                    policy=SandboxPolicy(trusted_python_netwall=True),
                )
            )

    def test_wall_timeout_kills_runaway(self):
        async def run():
            return await run_sandboxed(
                ["sleep", "30"], policy=SandboxPolicy(wall_seconds=1)
            )

        result = asyncio.run(run())
        assert result.timed_out and not result.ok

    def test_nonzero_exit_raises(self):
        async def run():
            return await run_python_op("import sys; sys.exit(3)", {})

        with pytest.raises(RuntimeError, match="rc=3"):
            asyncio.run(run())

    def test_network_egress_is_blocked(self):
        code = r"""
import json
import socket
import subprocess
import sys

out = {}

try:
    sock = socket.socket()
    sock.settimeout(1)
    sock.connect(("1.1.1.1", 80))
    out["socket_blocked"] = False
except PermissionError as e:
    out["socket_blocked"] = True
    out["socket_error"] = str(e)
except OSError as e:
    out["socket_blocked"] = False
    out["socket_error"] = repr(e)

child = "import socket; s=socket.socket(); s.settimeout(1); s.connect(('1.1.1.1', 80))"
try:
    proc = subprocess.run(
        [sys.executable, "-I", "-c", child],
        capture_output=True,
        text=True,
        timeout=3,
    )
    out["subprocess_blocked"] = proc.returncode != 0 and (
        "PermissionError" in proc.stderr or "Operation not permitted" in proc.stderr
    )
    out["subprocess_returncode"] = proc.returncode
    out["subprocess_stderr"] = proc.stderr[:200]
except PermissionError as e:
    out["subprocess_blocked"] = True
    out["subprocess_error"] = str(e)

print(json.dumps(out))
"""

        async def run():
            return await run_python_op(code, {})

        out = asyncio.run(run())
        assert out["socket_blocked"], out
        assert out["subprocess_blocked"], out


class TestBroker:
    def test_sandboxed_op_completes_via_broker_without_keys(self, tmp_path, sock_dir):
        """End-to-end: child process gets a model completion through the
        broker (served from the response cache — no network, no keys leak)."""
        cache = ResponseCache(tmp_path / "c.db")
        req = LLMRequest(
            model="anthropic/claude-haiku-4-5",
            messages=[{"role": "user", "content": "broker test"}],
        )
        cache.put(
            request_key(req),
            LLMResponse(
                content="brokered!",
                data=None,
                tokens_in=1,
                tokens_out=1,
                cost=0.0,
                model=req.model,
            ),
        )

        async def run():
            router = ModelRouter(
                keys={"anthropic": "sk-ant-neverleaks"},
                cache=cache,
                cache_mode="replay_strict",
            )
            sock = str(sock_dir / "broker.sock")
            broker = KeyBroker(router, sock)
            await broker.start()
            try:
                code = CLIENT_SNIPPET + (
                    "\nimport json, os\n"
                    "resp = frisket_complete('anthropic/claude-haiku-4-5',"
                    " [{'role': 'user', 'content': 'broker test'}])\n"
                    "tcp_blocked = False\n"
                    "try:\n"
                    "    s = socket.socket(); s.settimeout(1); s.connect(('1.1.1.1', 80))\n"
                    "except PermissionError:\n"
                    "    tcp_blocked = True\n"
                    "print(json.dumps({'content': resp['content'],"
                    " 'env_has_key': any('API_KEY' in k for k in os.environ),"
                    " 'tcp_blocked': tcp_blocked}))\n"
                )
                return await run_python_op(code, {}, broker_endpoint=broker.endpoint)
            finally:
                await broker.stop()
                await router.aclose()

        out = asyncio.run(run())
        assert out["content"] == "brokered!"
        assert out["env_has_key"] is False
        assert out["tcp_blocked"] is True

    def test_broker_surfaces_errors_cleanly(self, tmp_path, sock_dir):
        async def run():
            router = ModelRouter(keys={"anthropic": "k"})
            sock = str(sock_dir / "b.sock")
            broker = KeyBroker(router, sock)
            await broker.start()
            try:
                code = CLIENT_SNIPPET + (
                    "\ntry:\n"
                    "    frisket_complete('hologram/q9000', [{'role':'user','content':'x'}])\n"
                    "    print('{\"raised\": false}')\n"
                    "except RuntimeError as e:\n"
                    "    import json; print(json.dumps({'raised': True, 'msg': str(e)[:50]}))\n"
                )
                return await run_python_op(code, {}, broker_endpoint=broker.endpoint)
            finally:
                await broker.stop()
                await router.aclose()

        out = asyncio.run(run())
        assert out["raised"] is True
        assert "no adapter" in out["msg"]


def test_client_snippet_rejects_non_loopback_tcp_host(tmp_path):
    """CLIENT_SNIPPET's frisket_complete must refuse to connect to any tcp://
    broker endpoint host other than the literal 127.0.0.1, even before
    attempting a connection. The wire contract permits only the exact
    ("127.0.0.1", port) address, and a client that trusted whatever host a
    (potentially tampered) FRISKET_BROKER_ENDPOINT carried would defeat the
    netwall's own loopback pinning."""
    code = CLIENT_SNIPPET + (
        "\nimport json\n"
        "try:\n"
        "    frisket_complete('m', [{'role': 'user', 'content': 'x'}])\n"
        "    print(json.dumps({'raised': False}))\n"
        "except RuntimeError as e:\n"
        "    print(json.dumps({'raised': True, 'msg': str(e)}))\n"
    )
    script = tmp_path / "probe.py"
    script.write_text(code)
    env = dict(os.environ)
    env["FRISKET_BROKER_ENDPOINT"] = "tcp://1.1.1.1:9?token=x"
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["raised"] is True
    assert "127.0.0.1" in out["msg"]


def test_keybroker_normalizes_relative_socket_path(tmp_path, monkeypatch):
    """The promised ``unix://`` wire form is always absolute; a relative
    socket_path must be normalized at construction, not carried through to
    an endpoint whose urlsplit(...).path a consumer would misparse (a bare
    relative filename becomes a URL *netloc* with an empty *path* --
    consumer could misparse it)."""
    monkeypatch.chdir(tmp_path)
    router = ModelRouter(keys={"anthropic": "k"})
    broker = KeyBroker(router, "relative.sock")
    assert Path(broker.socket_path).is_absolute()
    assert Path(broker.socket_path) == (tmp_path / "relative.sock").resolve()
