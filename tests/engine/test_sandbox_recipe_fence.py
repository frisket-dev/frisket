"""What a code recipe is actually confined by -- the true perimeter, pinned.

This file is the successor to `test_sandbox_is_not_a_security_boundary.py`,
which pinned the perimeter as it stood before the kernel fence: an audit hook
over two Python surfaces, nothing below them, and no filesystem confinement at
all. Those holes are closed for `map.python` recipes on Linux, so the pins are
inverted here rather than deleted -- the same five lines of `ctypes` that used
to reach the network are still executed below, and the same absolute-path read
of a secret is still attempted. What changed is the expected answer.

Executed against the real `map.python` path (`run_python_op`) with the default
`SandboxPolicy`, on Linux, these hold:

  HOLDS  the seccomp filter refuses every non-AF_UNIX socket, from the stdlib
         AND from `ctypes.CDLL("libc.so.6")`
  HOLDS  the filter refuses `execve`, and refuses `clone` unless CLONE_THREAD
         is set -- so threads work, `ctypes`' `fork()` does not, and the
         subprocess wall is real below Python
  HOLDS  Landlock confines the filesystem: reads and writes outside the
         interpreter/stdlib/site-packages and the child's own scratch
         directory are refused, `~/.frisket/secrets/master.key` included
  HOLDS  the CPython audit hook is still there underneath, as defense in depth
  HOLDS  the environment is scrubbed of provider keys (`SCRUB_PREFIXES`)
  HOLDS  a recipe that only needs to compute still works -- stdlib, C
         extensions, site-packages, threads, timezones and temporary files
  HOLDS  a kernel that cannot install the fence, or a syscall number that is
         wrong for it, REFUSES the recipe; no recipe code runs

  DOES NOT HOLD  where a BROKERED recipe's AF_UNIX socket may connect. Only
         the audit hook guards that, and ctypes walks past it. Kept out of
         production by a closure test in this file, not by the kernel.
  DOES NOT HOLD  file metadata. Landlock does not govern `stat`/`access`, so a
         recipe can still learn a path exists. It cannot read it.
  DOES NOT HOLD  source secrecy. `sys.path` is readable by construction, so
         recipe code can read site-packages and, on an editable install, the
         project's own `src/` tree. Data and keys are still refused.

Linux-only by construction: macOS gets the `sandbox-exec` network wall and no
filesystem confinement, Windows gets nothing below Python. Both are stated in
`engine/sandbox/fence.py` and logged once per process. A skip here is honest; a
failure is real.
"""

import asyncio
import errno
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from frisket.engine.sandbox import fence
from frisket.engine.sandbox.shim import SandboxPolicy, run_python_op

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="pins the Linux seccomp/Landlock fence; other platforms have no kernel fence",
)

POLICY = SandboxPolicy(cpu_seconds=10, wall_seconds=30, memory_mb=512)


def _run(code: str, payload: dict | None = None, **kwargs) -> dict:
    """Run `code` through the real map.python entry point and return its JSON.

    Deliberately NOT a stub of the shim: a test double one level up would skip
    the very machinery whose limits are the subject.
    """
    return asyncio.run(run_python_op(code, payload or {}, policy=POLICY, **kwargs))


def test_ctypes_libc_egress_is_refused_by_the_kernel_filter():
    """The bypass that used to work: five lines of libc, straight past Python.

    A loopback listener owned by this test is the target, so the proof is
    hermetic and needs no internet. The recipe asks libc for an AF_INET socket
    exactly as before; the seccomp filter refuses at `socket()`, which is why
    there is no fd to connect with. The parent's failed `accept()` is the
    second half of the assertion: nothing arrived.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    accepted: list[object] = []

    def accept_one() -> None:
        listener.settimeout(10)
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        accepted.append(conn)
        conn.close()

    # realtime: a real listener in a real thread is the proof that no TCP
    # connection arrives; a simulated clock cannot observe a kernel refusal.
    accepter = threading.Thread(  # realtime: see above
        target=accept_one, daemon=True
    )
    accepter.start()
    try:
        out = _run(
            "import ctypes, json, socket, struct\n"
            "libc = ctypes.CDLL('libc.so.6', use_errno=True)\n"
            f"port = {port}\n"
            "fd = libc.socket(socket.AF_INET, socket.SOCK_STREAM, 0)\n"
            # struct sockaddr_in: family in host order, port and addr in
            # network order, then 8 bytes of padding.
            "sa = (struct.pack('=H', socket.AF_INET) + struct.pack('!H', port)\n"
            "      + socket.inet_aton('127.0.0.1') + b'\\x00' * 8)\n"
            "rc = libc.connect(fd, sa, len(sa)) if fd >= 0 else None\n"
            "print(json.dumps({'fd': fd, 'socket_errno': ctypes.get_errno(),\n"
            "                  'connect_rc': rc}))\n"
        )
        accepter.join(timeout=10)
    finally:
        listener.close()

    assert out["fd"] == -1, (
        "libc.socket(AF_INET) succeeded -- the seccomp filter is not refusing "
        "inet sockets and the ctypes egress path is open again"
    )
    assert out["socket_errno"] == errno.EPERM
    assert out["connect_rc"] is None
    assert not accepted, "the child reached the listener; the kernel fence is gone"


def test_stdlib_socket_and_execve_are_refused_below_python():
    """Both walls, in the order a recipe meets them.

    `socket.socket()` now fails at creation with EPERM from the kernel rather
    than at `connect()` with the audit hook's message -- the wall moved down a
    layer, which is the point. `subprocess` is still caught by the audit hook
    first (it raises before the fork), and a raw `libc.execv` that never
    triggers an audit event is refused by the filter underneath.
    """
    out = _run(
        "import ctypes, json, socket, subprocess, sys\n"
        "res = {}\n"
        "try:\n"
        "    socket.socket()\n"
        "    res['socket'] = 'created'\n"
        "except OSError as e:\n"
        "    res['socket'] = [type(e).__name__, e.errno]\n"
        "try:\n"
        "    subprocess.run([sys.executable, '-c', 'pass'])\n"  # subprocess-boundary: the recipe child trying to spawn is the subject
        "    res['subprocess'] = 'spawned'\n"
        "except PermissionError as e:\n"
        "    res['subprocess'] = str(e)\n"
        "libc = ctypes.CDLL('libc.so.6', use_errno=True)\n"
        "argv = (ctypes.c_char_p * 2)(b'/bin/sh', None)\n"
        "res['execv_rc'] = libc.execv(b'/bin/sh', argv)\n"
        "res['execv_errno'] = ctypes.get_errno()\n"
        "print(json.dumps(res))\n"
    )
    assert out["socket"] == ["PermissionError", errno.EPERM]
    # Defense in depth: the Python-level wall is still the first thing hit.
    assert (
        out["subprocess"] == "subprocesses are disabled by the Frisket sandbox netwall"
    )
    assert out["execv_rc"] == -1, "raw execv was allowed; the filter is not in force"
    assert out["execv_errno"] == errno.EPERM


def test_audit_hook_is_the_only_thing_guarding_the_brokers_af_unix_socket(caplog):
    """DOES NOT HOLD: "AF_UNIX only to the broker" is an UNENFORCED promise.

    A run with a key broker gets AF_UNIX sockets from the filter -- that is how
    op code is supposed to request completions without holding a key. Keeping
    those sockets pointed at the broker is left entirely to the CPython audit
    hook, and this test exists to say so out loud, because nothing below Python
    can do it: seccomp cannot dereference `connect`'s sockaddr pointer, and
    Landlock ABI 4 does not govern unix-socket connects at all. So the Python
    road is closed and the `ctypes` road is open -- the exact shape of hole
    this whole file was written to eliminate, surviving in one place.

    Reachability, and why this is a pin and not a fix: no production caller
    passes a broker to `run_python_op` (pinned below by
    `test_no_production_code_recipe_is_given_a_key_broker`), so no shipped path
    creates a socket of any family. Closing it properly means either Landlock
    ABI 6 (`LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET`, kernel 6.12+) or handing the
    recipe a socket the trusted prelude already connected -- which needs the
    broker's one-request-per-connection protocol to change first. Invert this
    test when that lands; do not delete it.
    """
    received: list[bytes] = []

    # AF_UNIX pathname sockets have a 107-byte Linux cap.  Keep this endpoint
    # out of pytest's potentially long xdist tmp_path while retaining a secure,
    # automatically cleaned test-owned directory.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="fk-") as sock_dir:
        victim = Path(sock_dir) / "victim.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(victim))
        listener.listen(1)

        def accept_one() -> None:
            listener.settimeout(10)
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                received.append(conn.recv(64))

        # realtime: the proof is that bytes really arrive at a real listener.
        accepter = threading.Thread(  # realtime: see above
            target=accept_one, daemon=True
        )
        accepter.start()
        try:
            out = _run(
                "import ctypes, json, socket, struct\n"
                f"victim = {str(victim)!r}\n"
                "res = {}\n"
                "try:\n"
                "    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect(victim)\n"
                "    res['python_road'] = 'connected'\n"
                "except PermissionError as e:\n"
                "    res['python_road'] = str(e)\n"
                "libc = ctypes.CDLL('libc.so.6', use_errno=True)\n"
                "fd = libc.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)\n"
                "sa = struct.pack('=H', socket.AF_UNIX) + victim.encode() + b'\\x00'\n"
                "res['raw_connect'] = libc.connect(fd, sa, len(sa))\n"
                "libc.send(fd, b'REACHED-VIA-CTYPES', 18, 0)\n"
                "print(json.dumps(res))\n",
                broker_endpoint="unix:///tmp/frisket-test-broker-that-does-not-exist.sock",
            )
            accepter.join(timeout=10)
        finally:
            listener.close()

    # The Python road is closed, and the audit hook is what closes it.
    assert out["python_road"] == "network access is disabled by the Frisket sandbox"
    # The raw road is not. If this ever starts failing, the fence got stronger:
    # rewrite the test to assert the refusal and update the NOT-enforced list in
    # engine/sandbox/shim.py and fence.py.
    assert out["raw_connect"] == 0, (
        "libc.connect to a non-broker unix socket was refused -- good news, but "
        "the docstrings claiming it is NOT refused are now wrong"
    )
    assert received == [b"REACHED-VIA-CTYPES"]
    # ...and the run said so at the time, not only in a docstring.
    assert any(
        "AF_UNIX" in r.getMessage() and "audit hook alone" in r.getMessage()
        for r in caplog.records
    ), "a brokered recipe ran without warning that one wall is unenforced"


def test_no_production_code_recipe_is_given_a_key_broker():
    """The closure test that keeps the hole above out of production.

    `run_python_op(broker_endpoint=...)` is the only way to make a recipe
    child able to create a socket at all, and its AF_UNIX confinement is
    audit-hook-only (see above). Rather than promise that nobody will wire one
    up, assert it: this goes red the day a production call site appears, and
    the reader is sent here instead of discovering the gap in an incident.
    """
    import subprocess
    import sys as _sys

    root = Path(__file__).resolve().parents[2] / "src"
    hits = subprocess.run(  # subprocess-boundary: a source sweep, not a spawn assertion
        [
            "grep",
            "-rn",
            "--include=*.py",
            "-e",
            "broker_endpoint",
            str(root),
        ],
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    callers = [
        line
        for line in hits
        if "run_python_op" in line or "broker_endpoint=" in line.split(":", 2)[-1]
    ]
    offenders = [
        line
        for line in callers
        # shim.py and broker.py DEFINE the parameter and the endpoint; only a
        # caller that PASSES one to run_python_op opens the hole.
        if "/engine/sandbox/" not in line
    ]
    assert offenders == [], (
        "a production call site now hands a key broker to a code recipe, which "
        "makes AF_UNIX sockets creatable inside the fence with only the CPython "
        "audit hook confining them (see "
        "test_audit_hook_is_the_only_thing_guarding_the_brokers_af_unix_socket). "
        "Close that first: " + "\n".join(offenders)
    )
    assert _sys.platform == "linux"  # the sweep is only meaningful where the fence is


def test_no_socket_of_any_family_exists_without_a_broker():
    """The default `map.python` run has no broker, so it gets no sockets at all.

    This is what closes the one hole AF_UNIX would otherwise leave open on
    Landlock ABI 4 (which does not govern unix-socket connects): a recipe with
    no broker to talk to cannot create the socket in the first place.
    """
    out = _run(
        "import json, socket\n"
        "res = {}\n"
        "for name in ('AF_UNIX', 'AF_INET', 'AF_INET6', 'AF_NETLINK', 'AF_PACKET'):\n"
        "    try:\n"
        "        socket.socket(getattr(socket, name), socket.SOCK_DGRAM)\n"
        "        res[name] = 'created'\n"
        "    except OSError as e:\n"
        "        res[name] = e.errno\n"
        "print(json.dumps(res))\n"
    )
    assert set(out.values()) == {errno.EPERM}, out


def test_files_outside_the_fence_are_refused_in_both_directions(tmp_path):
    """The filesystem hole, closed. Same probe as before, opposite answer.

    `tmp_path` stands in for `~/.frisket/secrets/master.key`: an absolute path
    outside the child's scratch cwd and outside any granted root. The recipe
    reads it, writes next to it, and lists the directory; Landlock refuses all
    three. `cwd` is still the scratch directory, and is still writable --
    a fence that stopped a recipe writing its own temporary files would be
    useless.
    """
    secret = tmp_path / "master.key"
    secret.write_bytes(b"\x01" * 32)
    dropped = tmp_path / "written-by-recipe.txt"

    out = _run(
        "import json, os, pathlib\n"
        f"secret = pathlib.Path({str(secret)!r})\n"
        f"dropped = pathlib.Path({str(dropped)!r})\n"
        "res = {}\n"
        "for label, fn in (('read', lambda: secret.read_bytes()),\n"
        "                  ('write', lambda: dropped.write_text('x')),\n"
        f"                  ('list', lambda: os.listdir({str(tmp_path)!r}))):\n"
        "    try:\n"
        "        fn()\n"
        "        res[label] = 'ALLOWED'\n"
        "    except OSError as e:\n"
        "        res[label] = [type(e).__name__, e.errno]\n"
        "cwd = pathlib.Path.cwd()\n"
        "(cwd / 'scratch-write.txt').write_text('recipes still need scratch')\n"
        "res['scratch'] = (cwd / 'scratch-write.txt').read_text()\n"
        "res['cwd_is_scratch'] = 'frisket-op-' in str(cwd)\n"
        "res['home_below_cwd'] = str(cwd) in os.environ['HOME']\n"
        "print(json.dumps(res))\n"
    )

    assert out["read"] == ["PermissionError", errno.EACCES]
    assert out["write"] == ["PermissionError", errno.EACCES]
    assert out["list"] == ["PermissionError", errno.EACCES]
    assert secret.read_bytes() == b"\x01" * 32
    assert not dropped.exists()
    assert out["scratch"] == "recipes still need scratch"
    assert out["cwd_is_scratch"] is True
    assert out["home_below_cwd"] is True


def test_etc_is_not_a_granted_root_because_a_key_can_live_there():
    """`/etc` is six named files, not a directory grant. Here is why.

    `team/security/secrets.py:_generated_key_path` honours
    FRISKET_SECRETS_KEY_DIR / FRISKET_DATA_DIR / FRISKET_HOME, so an operator
    can legitimately put the master key under `/etc/frisket`. Granting `/etc`
    as a read root -- the obvious way to make glibc's loader cache, timezone
    and passwd lookups work -- would hand that key to every recipe on exactly
    the deployments most likely to have configured it. So the fence names the
    files instead, and this test is what keeps the shortcut from creeping back.
    """
    out = _run(
        "import ctypes, getpass, json, os\n"
        "res = {}\n"
        "for label, path in (('list', '/etc'), ('shadow', '/etc/shadow'),\n"
        "                    ('planted', '/etc/frisket/secrets/master.key')):\n"
        "    try:\n"
        "        os.listdir(path) if label == 'list' else open(path, 'rb').read()\n"
        "        res[label] = 'ALLOWED'\n"
        "    except OSError as e:\n"
        "        res[label] = e.errno\n"
        "ctypes.CDLL('libm.so.6')\n"
        "res['dlopen'] = 'ok'\n"
        "res['getuser'] = bool(getpass.getuser())\n"
        "print(json.dumps(res))\n"
    )
    assert out["list"] == errno.EACCES
    assert out["shadow"] == errno.EACCES
    # ENOENT is fine for a path that does not exist here; EACCES is fine too.
    # What must never appear is 'ALLOWED'.
    assert out["planted"] != "ALLOWED"
    # ...and the six named files still do their job.
    assert out["dlopen"] == "ok"
    assert out["getuser"] is True


def test_metadata_is_still_visible_which_landlock_does_not_govern(tmp_path):
    """The limit that survives, written down so nobody has to rediscover it.

    Landlock (ABI 4) restricts opening, reading, writing and executing. It does
    not restrict `stat`, `access` or `readlink`. A recipe can therefore still
    probe for the existence and size of a file it will never be able to read.
    If a future kernel or a future ruleset closes this, invert the assertion --
    do not delete it.
    """
    secret = tmp_path / "master.key"
    secret.write_bytes(b"\x01" * 32)
    out = _run(
        "import json, os\n"
        f"st = os.stat({str(secret)!r})\n"
        "print(json.dumps({'size': st.st_size}))\n"
    )
    assert out["size"] == 32


def test_environment_is_scrubbed_of_provider_keys(monkeypatch):
    """Unchanged by the fence, and still the narrower of the two claims.

    Env scrubbing keeps keys out of the child's environment; Landlock is what
    keeps the child out of the file the keys are decrypted from.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-reach-the-child")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-reach-the-child")
    out = _run(
        "import json, os\n"
        "print(json.dumps({'leaked': sorted(\n"
        "    k for k, v in os.environ.items() if 'should-never-reach' in v\n"
        ")}))\n"
    )
    assert out["leaked"] == []


def test_a_recipe_that_only_computes_still_works():
    """A fence so tight nothing runs is as useless as no fence.

    This is the derived read set, exercised: the stdlib (including C extension
    modules and the timezone database), site-packages, and a temporary file --
    everything a per-row transform legitimately touches, given that its data
    arrives on stdin and leaves on stdout.
    """
    out = _run(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "import datetime, decimal, hashlib, sqlite3, tempfile, zoneinfo\n"
        "import pydantic\n"
        "res = {}\n"
        "res['doubled'] = payload['x'] * 2\n"
        "res['tz'] = datetime.datetime(2026, 7, 28, tzinfo=zoneinfo.ZoneInfo(\n"
        "    'America/New_York')).tzname()\n"
        "res['sqlite'] = sqlite3.connect(':memory:').execute('select 1').fetchone()[0]\n"
        "res['decimal'] = str(decimal.Decimal('1') / decimal.Decimal('3'))[:6]\n"
        "res['sha'] = hashlib.sha256(b'x').hexdigest()[:8]\n"
        "with tempfile.NamedTemporaryFile('w+') as fh:\n"
        "    fh.write('scratch'); fh.seek(0); res['tempfile'] = fh.read()\n"
        "res['site_packages'] = bool(pydantic.VERSION)\n"
        "print(json.dumps(res))\n",
        {"x": 21},
    )
    assert out == {
        "doubled": 42,
        "tz": "EDT",
        "sqlite": 1,
        "decimal": "0.3333",
        "sha": "2d711642",
        "tempfile": "scratch",
        "site_packages": True,
    }


# --- the refusal path ------------------------------------------------------
#
# Each of these breaks one fence in the child prelude and asserts the recipe
# refuses instead of running. They are the house rule ("disable a new fence
# once and confirm its test notices") kept permanently rather than performed
# once: if the prover ever stops proving, these go red.


def _with_broken_fence(monkeypatch, old: str, new: str) -> None:
    """Serve a child prelude with one line of the fence sabotaged."""
    source = fence._child_fence_source()
    assert old in source, f"fence source no longer contains {old!r}"
    monkeypatch.setattr(
        fence, "_child_fence_source", lambda: source.replace(old, new, 1)
    )


def test_recipe_refuses_when_the_kernel_has_no_landlock(monkeypatch, tmp_path):
    """Simulated old kernel: the ABI probe reports nothing. Refuse, do not run.

    The recipe body would create `canary` if it ever executed. It must not
    exist afterwards: the fence goes up before recipe code, so an unfenceable
    kernel is a refusal and never a partial run.
    """
    _with_broken_fence(
        monkeypatch,
        "    if abi < 1:",
        "    abi = 0\n    if abi < 1:",
    )
    canary = tmp_path / "recipe-ran"
    with pytest.raises(fence.SandboxEnforcementUnavailable) as caught:
        _run(f"import pathlib; pathlib.Path({str(canary)!r}).write_text('ran')")
    assert "landlock is unavailable on this kernel" in str(caught.value)
    assert "No recipe code ran" in str(caught.value)
    assert not canary.exists()


def test_recipe_refuses_when_seccomp_cannot_be_installed(monkeypatch, tmp_path):
    """Simulated kernel without CONFIG_SECCOMP_FILTER: the syscall fails."""
    _with_broken_fence(
        monkeypatch,
        "    if rc < 0:\n        raise _FrisketFenceUnavailable(\n"
        '            "seccomp(SECCOMP_SET_MODE_FILTER) failed',
        "    rc = -1\n    if rc < 0:\n        raise _FrisketFenceUnavailable(\n"
        '            "seccomp(SECCOMP_SET_MODE_FILTER) failed',
    )
    canary = tmp_path / "recipe-ran"
    with pytest.raises(fence.SandboxEnforcementUnavailable) as caught:
        _run(f"import pathlib; pathlib.Path({str(canary)!r}).write_text('ran')")
    assert "seccomp(SECCOMP_SET_MODE_FILTER) failed" in str(caught.value)
    assert not canary.exists()


def test_the_prover_notices_a_seccomp_filter_that_never_went_up(monkeypatch):
    """Skip the seccomp install entirely; the refusal probes must catch it.

    This is the red-proof for the syscall half, kept as a test. Without the
    prover, a wrong syscall number or a silently ignored filter would leave the
    recipe running unfenced and every other test in this file would still pass
    for the filesystem reasons.
    """
    _with_broken_fence(
        monkeypatch,
        "    _frisket_fence_install_seccomp(allow_unix_sockets, allow_exec)",
        "    pass",
    )
    with pytest.raises(fence.SandboxEnforcementUnavailable) as caught:
        _run("print('{}')")
    assert "socket() returned rc=" in str(caught.value)
    assert "the syscall filter is not in force" in str(caught.value)


@pytest.mark.parametrize("probe", ["clone", "unshare", "process_vm_readv", "execve"])
def test_the_prover_catches_a_wrong_number_for_a_non_socket_syscall(monkeypatch, probe):
    """A typo -- or AArch64 table drift -- must not sail past the prover.

    The prover's original single AF_INET probe certified the socket number and
    nothing else, so a wrong `execve` or `io_uring_setup` entry would have left
    that syscall allowed with every test still green. Corrupting one number at
    a time is the only way to show each probe is load-bearing rather than
    decorative.
    """
    import importlib

    child = importlib.import_module("frisket.engine.sandbox._child_fence")
    _, _, table = child._FRISKET_FENCE_ARCH_TABLES["x86_64"]
    _with_broken_fence(
        monkeypatch,
        f'"{probe}": {table[probe]},',
        f'"{probe}": {table[probe] + 4000},',
    )
    with pytest.raises(fence.SandboxEnforcementUnavailable) as caught:
        _run("print('{}')")
    assert repr(probe) in str(caught.value)
    assert "is wrong for this architecture" in str(caught.value)


def test_raw_fork_is_refused_but_threads_are_not(monkeypatch):
    """`clone` splits: a new thread is allowed, a new process is not.

    The audit hook refuses `os.fork`, but `ctypes` walks past it -- so before
    the filter learned to read clone's flags, a recipe could fork freely and
    fork-bomb the host. CLONE_THREAD is a bit in a scalar argument, which is
    exactly what seccomp CAN read, so the two cases are separable: threads
    (which numpy and friends genuinely need) still work, processes do not.
    """
    out = _run(
        "import ctypes, json, os, threading\n"
        "res = {}\n"
        "box = []\n"
        "t = threading.Thread(target=lambda: box.append(1)); t.start(); t.join()\n"  # realtime: a real thread is the subject
        "res['thread'] = box == [1]\n"
        "libc = ctypes.CDLL('libc.so.6', use_errno=True)\n"
        "ctypes.set_errno(0)\n"
        "pid = libc.fork()\n"
        "if pid == 0:\n"
        "    os._exit(0)\n"
        "res['libc_fork'] = [pid, ctypes.get_errno()]\n"
        "ctypes.set_errno(0)\n"
        "rc = libc.syscall(ctypes.c_long(56), ctypes.c_long(17),\n"
        "                  ctypes.c_void_p(None), ctypes.c_void_p(None),\n"
        "                  ctypes.c_void_p(None), ctypes.c_long(0))\n"
        "if rc == 0:\n"
        "    os._exit(0)\n"
        "res['raw_clone'] = [rc, ctypes.get_errno()]\n"
        "try:\n"
        "    p = os.fork()\n"
        "    if p == 0:\n"
        "        os._exit(0)\n"
        "    res['os_fork'] = 'FORKED'\n"
        "except PermissionError as e:\n"
        "    res['os_fork'] = str(e)\n"
        "print(json.dumps(res))\n"
    )
    assert out["thread"] is True, "the fence broke threading, which recipes use"
    assert out["libc_fork"] == [-1, errno.EPERM]
    assert out["raw_clone"] == [-1, errno.EPERM]
    assert out["os_fork"] == "subprocesses are disabled by the Frisket sandbox netwall"


def test_the_prover_notices_a_landlock_ruleset_that_never_went_up(monkeypatch):
    """Skip `landlock_restrict_self`; the canary directory must catch it.

    The ruleset is built and populated exactly as normal -- only the call that
    makes it take effect is dropped, which is the failure mode a wrong flag or
    a kernel regression would produce.
    """
    _with_broken_fence(
        monkeypatch,
        "        rc = libc.syscall(\n"
        "            _FRISKET_FENCE_LANDLOCK_RESTRICT_SELF,",
        "        rc = 0\n"
        "        _unused = (\n"
        "            _FRISKET_FENCE_LANDLOCK_RESTRICT_SELF,",
    )
    with pytest.raises(fence.SandboxEnforcementUnavailable) as caught:
        _run("print('{}')")
    assert "was still readable" in str(caught.value)


def test_a_networked_code_recipe_is_refused_outright():
    """There is no unfenced shape of this entry point to fall back to."""
    with pytest.raises(ValueError, match="does not run code recipes with network"):
        asyncio.run(
            run_python_op("print('{}')", {}, policy=SandboxPolicy(allow_network=True))
        )


# --- the contract the two halves agree on ---------------------------------


def test_every_architecture_table_refuses_the_same_syscalls():
    """Adding a syscall to one architecture and not the other goes red here.

    Enumeration is the failure mode: the x86-64 and AArch64 tables are two
    spellings of one decision, and nothing else would notice them drifting.
    """
    import importlib

    child = importlib.import_module("frisket.engine.sandbox._child_fence")
    tables = child._FRISKET_FENCE_ARCH_TABLES
    key_sets = {arch: frozenset(table) for arch, (_, _, table) in tables.items()}
    assert len(set(key_sets.values())) == 1, key_sets
    assert {"socket", "socketpair", "execve", "io_uring_setup"} <= next(
        iter(key_sets.values())
    )


def test_the_filter_only_narrows_when_unix_sockets_are_refused():
    """The two filter shapes differ, and both are well-formed BPF."""
    import importlib

    child = importlib.import_module("frisket.engine.sandbox._child_fence")
    arch, _, table = child._FRISKET_FENCE_ARCH_TABLES["x86_64"]
    with_unix = child._frisket_fence_build_filter(arch, table, True)
    without = child._frisket_fence_build_filter(arch, table, False)
    assert len(with_unix) % 8 == 0 and len(without) % 8 == 0
    assert len(with_unix) > len(without), (
        "the AF_UNIX-permitting filter must carry the extra domain check"
    )


def test_the_child_prelude_is_the_source_of_the_child_fence_module():
    """The prelude ships the real file, not a copy that can drift from it."""
    source = Path(
        __import__("frisket.engine.sandbox._child_fence", fromlist=["x"]).__file__
    ).read_text()
    assert fence._child_fence_source() == source
    prelude = fence.linux_recipe_prelude(allow_unix_sockets=False)
    assert prelude.startswith(source)
    assert "_frisket_fence_install(False)" in prelude
    assert fence.FENCE_UNAVAILABLE_MARKER in prelude


def test_platform_matrix_is_stated_and_only_linux_gets_a_prelude(monkeypatch):
    """Windows and macOS get no prelude -- and must not get one silently."""
    assert fence.recipe_prelude(allow_unix_sockets=False) != ""  # linux, per skipif
    assert set(fence._UNENFORCED_PLATFORM_NOTES) == {"darwin", "win32"}
    for platform, note in fence._UNENFORCED_PLATFORM_NOTES.items():
        assert "seccomp + Landlock" in note
        monkeypatch.setattr(fence.sys, "platform", platform)
        assert fence.recipe_prelude(allow_unix_sockets=False) == "", (
            f"{platform} would receive the Linux prelude, which its kernel "
            "cannot install"
        )


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_an_unfenced_platform_says_so_once(monkeypatch, caplog, platform):
    """The decided posture for macOS/Windows is 'run, but never quietly'.

    Refusing there would delete a working feature from every developer on a
    platform Frisket does not deploy on; staying silent would repeat the badge
    that claimed a sandbox nobody had. So: one warning naming what is missing,
    once per process, and this test is what stops it being dropped.
    """
    monkeypatch.setattr(fence, "_warned_platforms", set())
    monkeypatch.setattr(fence.sys, "platform", platform)
    with caplog.at_level("WARNING", logger=fence.logger.name):
        fence.warn_if_unenforced()
        fence.warn_if_unenforced()
    assert len(caplog.records) == 1, "the warning must be once per process, not per row"
    assert "NO" in caplog.records[0].getMessage()

    caplog.clear()
    monkeypatch.setattr(fence.sys, "platform", "linux")
    with caplog.at_level("WARNING", logger=fence.logger.name):
        fence.warn_if_unenforced()
    assert caplog.records == [], "Linux is enforced; it must not warn"


def test_refusal_reason_reads_only_its_own_marker():
    assert fence.refusal_reason("traceback\nboom\n") is None
    assert (
        fence.refusal_reason(
            f"noise\n{fence.FENCE_UNAVAILABLE_MARKER} RuntimeError: no landlock\n"
        )
        == "RuntimeError: no landlock"
    )


def test_json_round_trip_still_works_through_the_fence():
    """The one thing every recipe does, end to end, with the fence installed."""
    assert _run(
        "import json, sys; d = json.load(sys.stdin); print(json.dumps({'n': d['n'] + 1}))",
        {"n": 41},
    ) == {"n": 42}
