"""Interactive sandbox transport contract.

The fixture child is deliberately tiny and model-free. It speaks only the
four-byte length-prefix transport so these tests prove process ownership,
framing, deadlines, bounded diagnostics, and teardown independently of the
Parakeet JSON protocol layered on top.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

import frisket.engine.sandbox.shim as sandbox_shim
from frisket.engine.sandbox.shim import (
    SandboxProcessCancelledError,
    SandboxTeardownError,
    SandboxedProcess,
    open_sandboxed_process,
    run_sandboxed,
    run_sandboxed_stdout_lines,
)

# realtime: this suite drives a real child process over a real pipe transport
# (frame exchanges, wall_seconds deadlines against a genuine subprocess) and
# spawns real descendant process trees to prove teardown — real-child-process
# supervision cannot be advanced by a manual clock. The `survived`-never
# negatives are now deterministic —
# `_assert_no_survivor` rendezvouses with the descendant's DEATH (kernel pid
# probe on the pid the descendant publishes as its `started` marker) before
# a single marker check. What keeps the file on the real clock is
# irreducible: wall_seconds deadlines exercised against a genuinely blocked
# real subprocess (a manual clock cannot make a real child hit or miss a
# real pipe deadline).
pytestmark = pytest.mark.realtime


_WORKER = r"""
import json
import os
import subprocess
import sys
import time

inp = sys.stdin.buffer
out = sys.stdout.buffer
err = sys.stderr.buffer
ignore_next = False
output_after_close = False

def read_exactly(size):
    chunks = []
    remaining = size
    while remaining:
        chunk = inp.read(remaining)
        if not chunk:
            raise EOFError
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)

def read_frame():
    prefix = inp.read(4)
    if not prefix:
        return None
    if len(prefix) != 4:
        raise EOFError
    return read_exactly(int.from_bytes(prefix, "big"))

def write_frame(payload, fragmented=False):
    packet = len(payload).to_bytes(4, "big") + payload
    if fragmented:
        for byte in packet:
            out.write(bytes([byte]))
            out.flush()
        return
    out.write(packet)
    out.flush()

def spawn_marker_tree(cfg, *, block_root):
    prelude = ""
    if cfg.get("ignore_term"):
        prelude = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    # First act: atomically publish the descendant's own pid as the
    # `started` marker (the death-rendezvous identity for tests).
    code = (
        prelude
        + "import os\n"
        + "open(%r, 'w').write(str(os.getpid()))\n" % (cfg["started"] + ".tmp")
        + "os.replace(%r, %r)\n" % (cfg["started"] + ".tmp", cfg["started"])
        + "import time\n"
        + "time.sleep(%r)\n" % cfg["nap"]
        + "open(%r, 'w').write('survived')\n" % cfg["survived"]
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if block_root:
        time.sleep(300)
    return child.pid

while True:
    payload = read_frame()
    if payload is None:
        break
    if payload[:1] == b"Z":
        out.close()
        open(payload[1:].decode(), "w").write("closing")
        time.sleep(300)
    if payload == b"X":
        if output_after_close:
            write_frame(b"forbidden-after-close")
        break
    if ignore_next:
        ignore_next = False
        continue
    op, body = payload[:1], payload[1:]
    if op == b"E":
        write_frame(body)
    elif op == b"F":
        write_frame(b"fragmented-response", fragmented=True)
    elif op == b"M":
        write_frame(b"first-buffered")
        write_frame(b"second-buffered")
        ignore_next = True
    elif op == b"H":
        write_frame(os.environ["HOME"].encode())
    elif op == b"R":
        write_frame(b"r" * int(body.decode("ascii")))
    elif op == b"U":
        out.write((64 * 1024 * 1024 + 1).to_bytes(4, "big"))
        out.flush()
        time.sleep(300)
    elif op == b"P":
        out.write(b"\x00\x00")
        out.flush()
        break
    elif op == b"Q":
        out.write((20).to_bytes(4, "big") + b"short")
        out.flush()
        break
    elif op == b"S":
        err.write(b"private-stderr-sentinel:" * 16384)
        err.flush()
        write_frame(b"stderr-drained")
    elif op == b"T":
        err.write(b"never-leak-this-stderr-sentinel" * 8192)
        err.flush()
        time.sleep(300)
    elif op == b"O":
        output_after_close = True
        write_frame(b"armed")
    elif op == b"D":
        pid = spawn_marker_tree(json.loads(body), block_root=False)
        write_frame(str(pid).encode("ascii"))
    elif op == b"C":
        spawn_marker_tree(json.loads(body), block_root=True)
    else:
        write_frame(b"unknown")
"""

_NO_READ_WORKER = "import time; time.sleep(300)"
_PARENT_FRAME_LIMIT = 1024 * 1024
_STDERR_TAIL_LIMIT = 64 * 1024


def _argv(code: str = _WORKER) -> list[str]:
    return [sys.executable, "-c", code]


def _tree_config(
    tmp_path: Path, *, nap: float = 1.0, ignore_term: bool = False
) -> tuple[bytes, Path, Path]:
    started = tmp_path / "started.marker"
    survived = tmp_path / "survived.marker"
    body = json.dumps(
        {
            "started": str(started),
            "survived": str(survived),
            "nap": nap,
            "ignore_term": ignore_term,
        }
    ).encode()
    return body, started, survived


async def _await_path(path: Path, *, bound: float = 5.0) -> None:
    deadline = time.monotonic() + bound
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"fixture never reached marker: {path}")
        await asyncio.sleep(0.01)


def _assert_no_survivor(started: Path, survived: Path, *, watch_bound: float) -> None:
    """Deterministic 'never' (deterministic-time contract): rendezvous with
    the descendant's DEATH — a kernel signal-0 probe on the pid it published
    as its ``started`` marker (a positive await, generous bound) — then check
    ``survived`` exactly once. With the only possible writer proven dead,
    absence is a fact, not a window raced against a real clock. Non-POSIX
    has no safe signal-0 probe (``os.kill`` is TerminateProcess on Windows),
    so the bounded watch window remains the irreducible fallback there."""
    if os.name != "posix":
        deadline = time.monotonic() + watch_bound
        while time.monotonic() < deadline:
            assert not survived.exists(), (
                f"owned sandbox descendant survived: {survived}"
            )
            time.sleep(0.02)
        return
    pid = int(started.read_text())
    deadline = time.monotonic() + 15.0
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(f"sandbox descendant {pid} outlived teardown")
        time.sleep(0.02)
    assert not survived.exists(), f"owned sandbox descendant survived: {survived}"


@pytest.mark.skipif(os.name != "posix", reason="signal-0 reap proof is POSIX-only")
def test_stdout_line_over_frame_budget_refuses_and_reaps_child(tmp_path):
    lines = []
    child = (
        "import os, sys, time; "
        "print(os.getpid(), flush=True); "
        f"sys.stdout.buffer.write(b'x' * {sandbox_shim._SANDBOX_CHILD_FRAME_MAX_BYTES + 1}); "
        "sys.stdout.buffer.flush(); time.sleep(300)"
    )
    with pytest.raises(ValueError, match="exceed"):
        asyncio.run(
            run_sandboxed_stdout_lines(
                [sys.executable, "-c", child],
                on_stdout_line=lines.append,
                scratch_dir=tmp_path,
            )
        )
    assert len(lines) == 1  # The oversized frame is never delivered.
    with pytest.raises(ProcessLookupError):
        os.kill(int(lines[0]), 0)
    assert not list(tmp_path.glob(".frisket-home-*"))


def test_fragmented_and_buffered_frames_close_cleanly_and_remove_private_home(
    tmp_path,
):
    scratch = tmp_path / "caller-scratch"

    async def run() -> tuple[SandboxedProcess, Path, int]:
        process = await open_sandboxed_process(_argv(), scratch_dir=scratch)
        assert await process.exchange_frame(b"F", wall_seconds=2) == (
            b"fragmented-response"
        )
        assert await process.exchange_frame(b"M", wall_seconds=2) == b"first-buffered"
        assert await process.exchange_frame(b"ignored", wall_seconds=2) == (
            b"second-buffered"
        )
        child_home = Path((await process.exchange_frame(b"H", wall_seconds=2)).decode())
        assert child_home.is_dir()
        return process, child_home, await process.close(b"X", wall_seconds=2)

    process, child_home, returncode = asyncio.run(run())
    assert returncode == 0
    assert process.closed
    assert scratch.is_dir(), "caller-owned scratch must remain"
    assert not child_home.exists(), "private child HOME must be removed on close"
    assert not list(scratch.glob(".frisket-home-*"))


def test_outbound_limit_accepts_exact_boundary_and_rejects_one_byte_over():
    async def run() -> None:
        process = await open_sandboxed_process(_argv())
        payload = b"E" + (b"x" * (_PARENT_FRAME_LIMIT - 1))
        assert await process.exchange_frame(payload, wall_seconds=3) == payload[1:]
        with pytest.raises(ValueError, match="1048576-byte limit"):
            await process.exchange_frame(payload + b"x", wall_seconds=3)
        assert not process.closed, "pre-write validation must not poison the session"
        assert await process.close(b"X", wall_seconds=2) == 0

    asyncio.run(run())


def test_inbound_limit_accepts_exact_boundary_and_rejects_one_byte_over():
    async def run() -> None:
        exact = await open_sandboxed_process(_argv())
        assert (
            await exact.exchange_frame(b"R4096", wall_seconds=2, response_limit=4096)
            == b"r" * 4096
        )
        assert await exact.close(b"X", wall_seconds=2) == 0

        over = await open_sandboxed_process(_argv())
        with pytest.raises(ValueError, match="configured response limit"):
            await over.exchange_frame(b"R4097", wall_seconds=2, response_limit=4096)
        assert over.closed

        global_over = await open_sandboxed_process(_argv())
        with pytest.raises(ValueError, match="configured response limit"):
            await global_over.exchange_frame(b"U", wall_seconds=2)
        assert global_over.closed

    asyncio.run(run())


@pytest.mark.parametrize(
    ("frame", "message"),
    [(b"P", "frame prefix"), (b"Q", "frame payload")],
)
def test_partial_frame_eof_is_fatal_and_tears_down(frame, message):
    async def run() -> SandboxedProcess:
        process = await open_sandboxed_process(_argv())
        with pytest.raises(EOFError, match=message):
            await process.exchange_frame(frame, wall_seconds=2)
        return process

    assert asyncio.run(run()).closed


def test_one_deadline_covers_a_blocked_stdin_drain():
    async def run() -> SandboxedProcess:
        process = await open_sandboxed_process(_argv(_NO_READ_WORKER))
        with pytest.raises(TimeoutError):
            await process.exchange_frame(b"x" * _PARENT_FRAME_LIMIT, wall_seconds=0.1)
        return process

    assert asyncio.run(run()).closed


def test_large_stderr_is_drained_into_a_fixed_tail_without_logging(caplog):
    async def run() -> SandboxedProcess:
        process = await open_sandboxed_process(_argv())
        assert await process.exchange_frame(b"S", wall_seconds=3) == b"stderr-drained"
        assert await process.close(b"X", wall_seconds=2) == 0
        return process

    process = asyncio.run(run())
    assert process.stderr_tail_size == _STDERR_TAIL_LIMIT
    assert "private-stderr-sentinel" not in caplog.text


def test_raw_stderr_never_enters_timeout_or_logs(caplog):
    async def run() -> tuple[SandboxedProcess, str]:
        process = await open_sandboxed_process(_argv())
        with pytest.raises(TimeoutError) as caught:
            await process.exchange_frame(b"T", wall_seconds=0.2)
        return process, str(caught.value)

    process, message = asyncio.run(run())
    assert process.closed
    assert "never-leak-this-stderr-sentinel" not in message
    assert "never-leak-this-stderr-sentinel" not in caplog.text


def test_incremental_stderr_drain_failure_is_session_fatal():
    class DrainFailure(OSError):
        pass

    async def run() -> SandboxedProcess:
        process = await open_sandboxed_process(_argv())
        process._stderr_task.cancel()
        try:
            await process._stderr_task
        except asyncio.CancelledError:
            pass

        async def fail_drain():
            raise DrainFailure("diagnostic pipe failed")

        process._stderr_task = asyncio.create_task(fail_drain())
        with pytest.raises(DrainFailure, match="diagnostic pipe failed"):
            await process.exchange_frame(b"Eignored", wall_seconds=2)
        return process

    assert asyncio.run(run()).closed


def test_cooperative_cancel_kills_the_owned_tree_and_is_typed(tmp_path):
    body, started, survived = _tree_config(tmp_path, nap=1.0)

    async def run() -> SandboxedProcess:
        process = await open_sandboxed_process(
            _argv(), should_cancel=lambda: started.exists()
        )
        with pytest.raises(SandboxProcessCancelledError):
            await process.exchange_frame(b"C" + body, wall_seconds=30)
        return process

    process = asyncio.run(run())
    assert started.exists(), "the descendant tree must have formed"
    assert process.closed
    _assert_no_survivor(started, survived, watch_bound=1.2)


def test_pre_cancelled_open_spawns_nothing(monkeypatch):
    async def fail_spawn(*args, **kwargs):
        raise AssertionError("pre-cancelled open spawned a child")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    with pytest.raises(SandboxProcessCancelledError, match="before start"):
        asyncio.run(open_sandboxed_process(_argv(), should_cancel=lambda: True))


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimit wrapper contract")
def test_threaded_spawn_completes_without_preexec_and_keeps_rlimits(tmp_path):
    """A helper bounds the old fork deadlock without risking the test runner.

    The helper holds a lock in a sibling thread and replaces the old rlimit
    factory with a function that would acquire that lock in the post-fork
    child.  The former ``preexec_fn`` implementation blocks forever before
    spawn returns; this exec-wrapper implementation never invokes it.  The
    target also reports its effective limits, proving the wrapper did not make
    the safety repair by dropping the POSIX limit contract.
    """
    source_root = Path(__file__).parents[2] / "src"
    helper = r"""
import asyncio
import json
import threading
import sys
from frisket.engine.sandbox import shim

lock = threading.Lock()
held = threading.Event()
release = threading.Event()

def hold_lock():
    with lock:
        held.set()
        release.wait()

thread = threading.Thread(target=hold_lock)
thread.start()
assert held.wait(2)

def unsafe_preexec(_policy):
    return lock.acquire

shim._limits = unsafe_preexec
child = (
    "import json,resource; print(json.dumps({'cpu': resource.getrlimit(resource.RLIMIT_CPU), "
    "'memory': resource.getrlimit(resource.RLIMIT_AS)}))"
)

try:
    result = asyncio.run(
        shim.run_sandboxed(
            [sys.executable, "-c", child],
            policy=shim.SandboxPolicy(cpu_seconds=3, memory_mb=256),
        )
    )
    assert result.ok, result.stderr
    print(result.stdout, end="")
finally:
    release.set()
    thread.join(timeout=2)
"""
    environment = os.environ.copy()
    previous_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{source_root}{os.pathsep}{previous_pythonpath}"
        if previous_pythonpath
        else str(source_root)
    )
    process = subprocess.Popen(
        [sys.executable, "-c", helper],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        pytest.fail(
            "threaded sandbox spawn deadlocked before process ownership; "
            f"stdout={stdout!r} stderr={stderr!r}"
        )

    assert process.returncode == 0, stderr
    limits = json.loads(stdout)
    assert limits["cpu"] == [3, 3]
    if sys.platform != "darwin":
        assert limits["memory"] == [256 * 1024 * 1024, 256 * 1024 * 1024]


@pytest.mark.skipif(os.name == "nt", reason="POSIX rlimit wrapper contract")
def test_posix_limit_wrapper_reports_target_exec_failure():
    """The wrapper must surface a missing target instead of silently succeeding."""
    result = asyncio.run(
        run_sandboxed(
            ["/definitely-not-a-frisket-executable"],
            policy=sandbox_shim.SandboxPolicy(cpu_seconds=3, memory_mb=256),
        )
    )

    assert not result.ok
    assert result.returncode != 0
    assert "FRISKET_SANDBOX_BOOTSTRAP_ERROR: FileNotFoundError" in result.stderr


def test_cancellation_during_spawn_still_owns_reaps_and_cleans_child(
    tmp_path, monkeypatch
):
    scratch = tmp_path / "spawn-scratch"
    real_spawn = asyncio.create_subprocess_exec
    spawned = None

    async def run() -> asyncio.subprocess.Process:
        nonlocal spawned
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_spawn(*args, **kwargs):
            nonlocal spawned
            spawned = await real_spawn(*args, **kwargs)
            entered.set()
            await release.wait()
            return spawned

        monkeypatch.setattr(
            sandbox_shim.asyncio, "create_subprocess_exec", delayed_spawn
        )
        opening = asyncio.create_task(
            open_sandboxed_process(_argv(_NO_READ_WORKER), scratch_dir=scratch)
        )
        await asyncio.wait_for(entered.wait(), 5)
        opening.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
        assert spawned is not None
        return spawned

    proc = asyncio.run(run())
    assert proc.returncode is not None
    assert scratch.is_dir()
    assert not list(scratch.glob(".frisket-home-*"))


@pytest.mark.parametrize("streaming", [False, True], ids=["buffered", "streaming"])
def test_legacy_cancellation_during_spawn_owns_reaps_and_cleans_root(
    tmp_path, monkeypatch, streaming
):
    scratch = tmp_path / f"legacy-spawn-{streaming}"
    real_spawn = asyncio.create_subprocess_exec
    spawned = None

    async def run() -> asyncio.subprocess.Process:
        nonlocal spawned
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_spawn(*args, **kwargs):
            nonlocal spawned
            spawned = await real_spawn(*args, **kwargs)
            entered.set()
            await release.wait()
            return spawned

        monkeypatch.setattr(
            sandbox_shim.asyncio, "create_subprocess_exec", delayed_spawn
        )
        if streaming:
            operation = asyncio.create_task(
                run_sandboxed_stdout_lines(
                    _argv(_NO_READ_WORKER),
                    on_stdout_line=lambda _line: None,
                    scratch_dir=scratch,
                )
            )
        else:
            operation = asyncio.create_task(
                run_sandboxed(_argv(_NO_READ_WORKER), scratch_dir=scratch)
            )
        await asyncio.wait_for(entered.wait(), 5)
        operation.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert spawned is not None
        return spawned

    proc = asyncio.run(run())
    assert proc.returncode is not None
    assert scratch.is_dir()
    assert not list(scratch.glob(".frisket-home-*"))


@pytest.mark.parametrize("streaming", [False, True], ids=["buffered", "streaming"])
def test_legacy_ownership_failure_kills_reaps_and_preserves_error(
    tmp_path, monkeypatch, streaming
):
    scratch = tmp_path / f"legacy-ownership-{streaming}"
    state: dict[str, asyncio.subprocess.Process] = {}

    class OwnershipFailure(RuntimeError):
        pass

    class FailingController:
        def __init__(self, proc):
            state["proc"] = proc
            raise OwnershipFailure("fixture ownership failed")

    monkeypatch.setattr(sandbox_shim, "ProcessTreeController", FailingController)

    async def run() -> None:
        if streaming:
            await run_sandboxed_stdout_lines(
                _argv(_NO_READ_WORKER),
                on_stdout_line=lambda _line: None,
                scratch_dir=scratch,
            )
        else:
            await run_sandboxed(_argv(_NO_READ_WORKER), scratch_dir=scratch)

    with pytest.raises(OwnershipFailure, match="fixture ownership failed"):
        asyncio.run(run())
    assert state["proc"].returncode is not None
    assert scratch.is_dir()
    assert not list(scratch.glob(".frisket-home-*"))


def test_buffered_stdin_file_is_fed_after_ownership(tmp_path):
    payload = b"file-backed-input" * 8192
    source = tmp_path / "stdin.bin"
    source.write_bytes(payload)
    code = "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)"

    with source.open("rb") as stdin_file:
        result = asyncio.run(
            run_sandboxed(_argv(code), stdin_file=stdin_file, scratch_dir=tmp_path)
        )

    assert result.ok
    assert result.stdout.encode() == payload
    assert not list(tmp_path.glob(".frisket-home-*"))


def test_outer_task_cancel_kills_tree_then_reraises(tmp_path):
    body, started, survived = _tree_config(tmp_path, nap=1.0)

    async def run() -> SandboxedProcess:
        process = await open_sandboxed_process(_argv())
        exchange = asyncio.create_task(
            process.exchange_frame(b"C" + body, wall_seconds=30)
        )
        await _await_path(started)
        exchange.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exchange
        return process

    process = asyncio.run(run())
    assert process.closed
    _assert_no_survivor(started, survived, watch_bound=1.2)


def test_clean_root_exit_with_live_descendant_escalates_before_close_returns(tmp_path):
    # The descendant ignores TERM, so its marker deadline must comfortably
    # outlast the 1-second TERM grace even on a loaded CI host.
    body, started, survived = _tree_config(tmp_path, nap=4.0, ignore_term=True)

    async def run() -> SandboxedProcess:
        process = await open_sandboxed_process(_argv())
        await process.exchange_frame(b"D" + body, wall_seconds=2)
        await _await_path(started)
        assert await process.close(b"X", wall_seconds=2) == 0
        return process

    process = asyncio.run(run())
    assert process.closed
    _assert_no_survivor(started, survived, watch_bound=4.2)


def test_output_after_close_is_fatal_but_tree_is_still_reaped():
    async def run() -> SandboxedProcess:
        process = await open_sandboxed_process(_argv())
        assert await process.exchange_frame(b"O", wall_seconds=2) == b"armed"
        with pytest.raises(RuntimeError, match="output after close"):
            await process.close(b"X", wall_seconds=2)
        return process

    assert asyncio.run(run()).closed


def test_cancelling_graceful_close_does_not_cancel_retained_reap(tmp_path):
    closing = tmp_path / "closing.marker"

    async def run() -> SandboxedProcess:
        process = await open_sandboxed_process(_argv())
        close_task = asyncio.create_task(
            process.close(b"Z" + str(closing).encode(), wall_seconds=30)
        )
        await _await_path(closing)
        close_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await close_task
        assert not process._reap_task.cancelled()
        assert process._reap_task.done()
        assert isinstance(process._reap_task.result(), int)
        return process

    assert asyncio.run(run()).closed


def test_unprovable_tree_raises_strict_teardown_error_after_cleanup(
    tmp_path, monkeypatch
):
    scratch = tmp_path / "scratch"

    async def run() -> SandboxedProcess:
        process = await open_sandboxed_process(_argv(), scratch_dir=scratch)

        async def never_gone(timeout):
            return False

        monkeypatch.setattr(process._controller, "wait_tree_gone", never_gone)
        with pytest.raises(SandboxTeardownError, match="remained live"):
            await process.abort()
        return process

    process = asyncio.run(run())
    assert not process.closed
    assert scratch.is_dir()
    assert list(scratch.glob(".frisket-home-*")), (
        "containment resources must remain owned after unprovable teardown"
    )
    with pytest.raises(SandboxTeardownError):
        asyncio.run(process.abort())


def test_verified_teardown_waits_for_reap_task_without_cancelling_it():
    class GoneController:
        def signal_tree(self, signal_number):
            return None

        async def wait_tree_gone(self, timeout):
            return True

    async def run() -> None:
        release_reap = asyncio.Event()

        async def reap():
            await release_reap.wait()
            return 0

        reap_task = asyncio.create_task(reap())
        teardown = asyncio.create_task(
            sandbox_shim._terminate_and_reap(GoneController(), reap_task=reap_task)
        )
        await asyncio.sleep(0)
        assert not teardown.done(), "tree proof alone must not skip direct-child reap"
        assert not reap_task.cancelled()
        release_reap.set()
        await teardown
        assert reap_task.result() == 0

    asyncio.run(run())
