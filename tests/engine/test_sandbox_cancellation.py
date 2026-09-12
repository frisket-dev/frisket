"""Sandbox process-tree cancellation.

The canonical proof uses a repository-owned tiny worker and filesystem markers,
never real models or timing-only sleeps: a sandboxed PARENT spawns a GRANDCHILD
that writes a ``started`` gate marker, naps, and only then writes a ``survived``
marker. Tests ASSERT the grandchild started (the tree truly formed — no vacuous
pass) and then, after the sandbox call returns (cancelled / timed out /
outer-cancelled), poll with a short monotonic bound and assert ``survived``
NEVER appears — i.e. the whole owned tree was actually torn down.

Both entry points are exercised because their teardown implementations differ,
and the hard ownership cases (root exits before its descendants; a TERM-ignoring
grandchild; a cancellation arriving mid-teardown) are covered explicitly.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from frisket.engine.sandbox.shim import (
    SandboxPolicy,
    run_sandboxed,
    run_sandboxed_stdout_lines,
)

# realtime: a manual clock cannot supervise a sandboxed parent that spawns a
# real grandchild via subprocess.Popen. The `survived`-never negatives are now
# deterministic — `_assert_no_survivor` rendezvouses with the grandchild's
# DEATH (kernel pid probe) before a single marker check — so what keeps this
# file on the real clock is irreducible: the `elapsed < _TEARDOWN_BOUND`
# promptness bounds measure how long a REAL process tree takes to tear down
# (invariant 6), and a real duration can only be measured on a real clock.
pytestmark = pytest.mark.realtime

# The grandchild atomically publishes ITS OWN PID as the `started` marker the
# instant it runs (proving the tree formed down to the grandchild, and giving
# tests a kernel-checkable identity for the death rendezvous), optionally
# ignores SIGTERM, naps, then writes `survived`. The parent spawns it,
# announces a stdout line, and either stays alive or (root_exits_early) exits
# after its started gate — orphaning the grandchild, the case where the leader dies
# before its descendants.
_TREE_PARENT = r"""
import json, os, subprocess, sys, time

cfg = json.load(sys.stdin)
started = cfg["started"]
survived = cfg["survived"]
nap = cfg["grandchild_nap"]

prelude = ""
if cfg.get("grandchild_ignore_term"):
    prelude = "import signal\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"

child_code = (
    prelude
    + "import os\n"
    + "open(%r, 'w').write(str(os.getpid()))\n" % (started + ".tmp")
    + "os.replace(%r, %r)\n" % (started + ".tmp", started)
    + "import time\n"
    + "time.sleep(%r)\n" % nap
    + "open(%r, 'w').write('survived')\n" % survived
)
proc = subprocess.Popen([sys.executable, "-c", child_code])

sys.stdout.write("spawned %d\n" % proc.pid)
sys.stdout.flush()

if cfg.get("root_ignore_term"):
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

if cfg.get("root_exits_early"):
    # A guardian cleans descendants as soon as this root exits. Ensure the
    # grandchild has installed its TERM handler and published the cancel gate
    # before leaving; otherwise cleanup can legitimately win before it starts.
    deadline = time.monotonic() + 10
    while not os.path.exists(started):
        if proc.poll() is not None or time.monotonic() >= deadline:
            raise RuntimeError("grandchild did not reach its started gate")
        time.sleep(0.01)
    sys.exit(0)

time.sleep(300)
"""

_GRANDCHILD_NAP = 1.0  # a survivor writes its marker this soon after `started`
_MARKER_WATCH_BOUND = 2.5  # ... so a > nap watch window catches a survivor
# Timeout tests fire the wall well before the grandchild's nap ends, so the
# kill lands with a comfortable margin (a survivor would still write at ~nap).
_TIMEOUT_WALL_SECONDS = 0.5
# A TERM-ignoring grandchild is only reached by the KILL escalation, which lands
# after the ~1s TERM grace, so its nap must clearly outlast that; the watch
# window must in turn outlast the nap to catch a would-be survivor.
_IGNORE_TERM_NAP = 3.0
_IGNORE_TERM_WATCH = 3.5
# Cancellation returns far below this; the worker wall is set higher still, so
# asserting teardown elapsed < this proves invariant 6 (bounded teardown)
# rather than assuming it.
_WORKER_WALL = 30
_TEARDOWN_BOUND = 15.0


def _config(tmp_path: Path, **flags) -> tuple[dict, Path, Path]:
    started = tmp_path / "started.marker"
    survived = tmp_path / "survived.marker"
    cfg = {
        "started": str(started),
        "survived": str(survived),
        "grandchild_nap": flags.pop("grandchild_nap", _GRANDCHILD_NAP),
        **flags,
    }
    return cfg, started, survived


def _argv() -> list[str]:
    return [sys.executable, "-c", _TREE_PARENT]


def _assert_marker_never(marker: Path, *, bound: float = _MARKER_WATCH_BOUND) -> None:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        assert not marker.exists(), f"grandchild survived teardown: {marker}"
        time.sleep(0.05)


def _assert_no_survivor(
    started: Path, survived: Path, *, watch_bound: float = _MARKER_WATCH_BOUND
) -> None:
    """Deterministic 'never' (deterministic-time contract): rendezvous with
    the grandchild's DEATH, then check ``survived`` exactly once — with the
    only possible writer proven dead, absence is a fact, not a window raced
    against a real clock.

    The grandchild's first act atomically publishes its own pid as the
    ``started`` marker, so a kernel signal-0 probe (positive await, generous
    bound) proves it is gone. If ``started`` never appeared, the grandchild
    was torn down before its FIRST act, and ``survived`` — written strictly
    after ``started`` in program order — is unreachable: a single check
    suffices there too. Non-POSIX has no safe signal-0 probe (``os.kill``
    is TerminateProcess on Windows), so the bounded watch window remains
    the irreducible fallback."""
    if os.name != "posix":
        _assert_marker_never(survived, bound=watch_bound)
        return
    if started.exists():
        pid = int(started.read_text())
        deadline = time.monotonic() + 15.0
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() >= deadline:
                raise AssertionError(f"grandchild {pid} outlived teardown")
            time.sleep(0.02)
    assert not survived.exists(), f"grandchild survived teardown: {survived}"


async def _await_marker(marker: Path, *, bound: float = 10.0) -> None:
    deadline = time.monotonic() + bound
    while not marker.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"tree never reached its gate marker: {marker}")
        await asyncio.sleep(0.02)


async def _run_buffered(cfg, *, should_cancel=None, wall=_WORKER_WALL):
    return await run_sandboxed(
        _argv(),
        policy=SandboxPolicy(wall_seconds=wall),
        stdin_data=json.dumps(cfg).encode(),
        should_cancel=should_cancel,
    )


async def _run_streaming(cfg, *, should_cancel=None, wall=_WORKER_WALL, lines=None):
    return await run_sandboxed_stdout_lines(
        _argv(),
        on_stdout_line=(lines.append if lines is not None else (lambda _line: None)),
        policy=SandboxPolicy(wall_seconds=wall),
        stdin_data=json.dumps(cfg).encode(),
        should_cancel=should_cancel,
    )


# ---------------------------------------------------------------------------
# 1. Sandbox outcome + tree cleanup — both entry points.


def test_pre_cancellation_buffered_creates_no_process(tmp_path):
    cfg, started, survived = _config(tmp_path)
    result = asyncio.run(_run_buffered(cfg, should_cancel=lambda: True))
    assert result.cancelled and not result.ok and not result.timed_out
    assert not started.exists(), "a pre-cancelled run must not launch a worker"
    _assert_no_survivor(started, survived)


def test_pre_cancellation_streaming_creates_no_process(tmp_path):
    cfg, started, survived = _config(tmp_path)
    result = asyncio.run(_run_streaming(cfg, should_cancel=lambda: True))
    assert result.cancelled and not result.ok
    assert not started.exists()
    _assert_no_survivor(started, survived)


def test_mid_run_cancellation_buffered_kills_tree(tmp_path):
    cfg, started, survived = _config(tmp_path)
    t0 = time.monotonic()
    result = asyncio.run(_run_buffered(cfg, should_cancel=lambda: started.exists()))
    elapsed = time.monotonic() - t0
    assert result.cancelled and not result.ok and not result.timed_out
    assert started.exists(), "the grandchild must actually have run before cancel"
    assert elapsed < _TEARDOWN_BOUND, "teardown must be bounded (invariant 6)"
    _assert_no_survivor(started, survived)


def test_mid_run_cancellation_streaming_kills_tree(tmp_path):
    cfg, started, survived = _config(tmp_path)
    lines: list[str] = []
    t0 = time.monotonic()
    result = asyncio.run(
        _run_streaming(cfg, should_cancel=lambda: started.exists(), lines=lines)
    )
    elapsed = time.monotonic() - t0
    assert result.cancelled and not result.ok
    assert started.exists()
    assert any(line.startswith("spawned") for line in lines)
    assert elapsed < _TEARDOWN_BOUND
    _assert_no_survivor(started, survived)


def test_timeout_buffered_stays_timeout_and_kills_tree(tmp_path):
    cfg, started, survived = _config(tmp_path)
    result = asyncio.run(_run_buffered(cfg, wall=_TIMEOUT_WALL_SECONDS))
    # A timeout performs the same tree cleanup but stays classified a timeout,
    # never cancellation (invariant 3).
    assert result.timed_out and not result.cancelled and not result.ok
    assert started.exists(), "the tree must have formed before the wall fired"
    _assert_no_survivor(started, survived)


def test_timeout_streaming_stays_timeout_and_kills_tree(tmp_path):
    cfg, started, survived = _config(tmp_path)
    result = asyncio.run(_run_streaming(cfg, wall=_TIMEOUT_WALL_SECONDS))
    assert result.timed_out and not result.cancelled and not result.ok
    assert started.exists()
    _assert_no_survivor(started, survived)


# ---------------------------------------------------------------------------
# 1b. Ownership hard cases: the leader dies before its descendants,
# and a TERM-ignoring grandchild forces KILL escalation to reach the group/job.


@pytest.mark.parametrize("entry", ["buffered", "streaming"])
def test_root_exits_early_still_kills_orphaned_grandchild(tmp_path, entry):
    """The parent exits immediately after spawning a TERM-ignoring grandchild,
    so at teardown the leader is already dead. Cancellation must still reach the
    orphan — on POSIX the process group outlives its leader; on Windows the Job
    Object owns the tree regardless. (`taskkill /T` on a dead pid could not.)"""
    cfg, started, survived = _config(
        tmp_path,
        grandchild_nap=_IGNORE_TERM_NAP,
        root_exits_early=True,
        grandchild_ignore_term=True,
    )
    runner = _run_buffered if entry == "buffered" else _run_streaming
    t0 = time.monotonic()
    result = asyncio.run(runner(cfg, should_cancel=lambda: started.exists()))
    elapsed = time.monotonic() - t0
    assert result.cancelled and not result.ok
    assert started.exists()
    assert elapsed < _TEARDOWN_BOUND
    _assert_no_survivor(started, survived, watch_bound=_IGNORE_TERM_WATCH)


def test_term_ignoring_grandchild_forces_kill_escalation(tmp_path):
    """A live parent honors TERM but the grandchild ignores it; the bounded
    TERM -> KILL escalation must still reach the grandchild through the group."""
    cfg, started, survived = _config(
        tmp_path, grandchild_nap=_IGNORE_TERM_NAP, grandchild_ignore_term=True
    )
    t0 = time.monotonic()
    result = asyncio.run(_run_buffered(cfg, should_cancel=lambda: started.exists()))
    elapsed = time.monotonic() - t0
    assert result.cancelled and not result.ok
    assert started.exists()
    assert elapsed < _TEARDOWN_BOUND
    _assert_no_survivor(started, survived, watch_bound=_IGNORE_TERM_WATCH)


# ---------------------------------------------------------------------------
# 2. Exceptional cleanup — re-raise only after the owned tree is gone.


def test_outer_task_cancellation_kills_tree_then_reraises(tmp_path):
    cfg, started, survived = _config(tmp_path)

    async def go() -> None:
        task = asyncio.ensure_future(_run_buffered(cfg))
        await _await_marker(started)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    _assert_no_survivor(started, survived)


def _install_teardown_gate(monkeypatch):
    """Deterministically pause teardown so a test can cancel the outer task
    while cleanup is provably in flight — no fixed sleeps, and independent of
    any TERM-grace timing (Windows' Job terminate is instantaneous, so a
    grace-window sleep would be machine-dependent luck there). Returns two
    asyncio.Events: ``entered`` (set when teardown begins) and ``release`` (the
    test sets it to let the real teardown proceed)."""
    import frisket.engine.sandbox.shim as shim

    entered = asyncio.Event()
    release = asyncio.Event()
    real_terminate_and_reap = shim._terminate_and_reap

    async def gated(controller, *, pipe_tasks=(), reap_task=None):
        entered.set()
        await release.wait()
        return await real_terminate_and_reap(
            controller, pipe_tasks=pipe_tasks, reap_task=reap_task
        )

    monkeypatch.setattr(shim, "_terminate_and_reap", gated)
    return entered, release


def test_cancel_arriving_during_teardown_still_propagates_and_kills_tree(
    tmp_path, monkeypatch
):
    """A fresh outer cancellation arriving while cooperative teardown
    is in flight must not be swallowed into a normal SandboxResult — it has to
    surface as CancelledError, and the tree must still be gone. Synchronized
    deterministically via a teardown gate (no timing luck)."""
    cfg, started, survived = _config(tmp_path)
    entered, release = _install_teardown_gate(monkeypatch)

    async def go() -> None:
        task = asyncio.ensure_future(
            _run_buffered(cfg, should_cancel=lambda: started.exists())
        )
        await asyncio.wait_for(entered.wait(), 10)  # cooperative teardown reached
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    _assert_no_survivor(started, survived)


class _CallbackBoom(RuntimeError):
    pass


def test_buffered_callback_error_plus_cancel_during_teardown_ends_cancelled(
    tmp_path, monkeypatch
):
    """Finding B (buffered): a raising should_cancel triggers teardown; a fresh
    outer cancel arriving mid-teardown must make the task end CANCELLED (not
    failed with the callback error), and the tree must be gone."""
    cfg, started, survived = _config(tmp_path)
    entered, release = _install_teardown_gate(monkeypatch)

    def bad_cancel() -> bool:
        if started.exists():
            raise _CallbackBoom("cancel probe blew up")
        return False

    async def go() -> None:
        task = asyncio.ensure_future(_run_buffered(cfg, should_cancel=bad_cancel))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    _assert_no_survivor(started, survived)


def test_streaming_callback_error_plus_cancel_during_teardown_ends_cancelled(
    tmp_path, monkeypatch
):
    """Finding B (streaming): a raising on_stdout_line triggers teardown; a fresh
    outer cancel arriving mid-teardown must make the task end CANCELLED, tree
    gone."""
    cfg, started, survived = _config(tmp_path)
    entered, release = _install_teardown_gate(monkeypatch)

    def on_line(line: str) -> None:
        if line.startswith("spawned"):
            raise _CallbackBoom("line callback blew up")

    async def go() -> None:
        task = asyncio.ensure_future(
            run_sandboxed_stdout_lines(
                _argv(),
                on_stdout_line=on_line,
                policy=SandboxPolicy(wall_seconds=_WORKER_WALL),
                stdin_data=json.dumps(cfg).encode(),
            )
        )
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())
    _assert_no_survivor(started, survived)


def test_raising_should_cancel_kills_tree_then_reraises_original(tmp_path):
    cfg, started, survived = _config(tmp_path)

    def bad_cancel() -> bool:
        if started.exists():
            raise _CallbackBoom("cancel probe blew up")
        return False

    with pytest.raises(_CallbackBoom):
        asyncio.run(_run_buffered(cfg, should_cancel=bad_cancel))
    _assert_no_survivor(started, survived)


def test_raising_stdout_callback_kills_tree_then_reraises_original(tmp_path):
    cfg, started, survived = _config(tmp_path)

    def on_line(line: str) -> None:
        if line.startswith("spawned"):
            raise _CallbackBoom("line callback blew up")

    with pytest.raises(_CallbackBoom):
        asyncio.run(
            run_sandboxed_stdout_lines(
                _argv(),
                on_stdout_line=on_line,
                policy=SandboxPolicy(wall_seconds=_WORKER_WALL),
                stdin_data=json.dumps(cfg).encode(),
            )
        )
    _assert_no_survivor(started, survived)


# ---------------------------------------------------------------------------
# 3. Omitting should_cancel preserves existing behavior exactly.


def test_no_cancel_callable_runs_to_completion(tmp_path):
    result = asyncio.run(
        run_sandboxed(
            [sys.executable, "-c", "print('ok')"],
            policy=SandboxPolicy(wall_seconds=_WORKER_WALL),
        )
    )
    assert result.ok and not result.cancelled and not result.timed_out
    assert result.stdout.strip() == "ok"
