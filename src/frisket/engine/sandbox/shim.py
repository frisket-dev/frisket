"""Sandbox shim: the complete op runtime interface.

Every python/subprocess op executes through here. Read the lists below as the
WHOLE of what this layer does.

There are two different perimeters in this file, and confusing them is the
mistake to avoid:

  CODE RECIPES (`run_python_op`, behind `map.python`) are fenced by the
  kernel on Linux -- seccomp-BPF plus Landlock, installed in the recipe child
  before recipe code runs, refusing the run outright if the kernel cannot do
  it. See `fence.py` for the platform matrix and `_child_fence.py` for the
  filter and the ruleset. macOS gets the sandbox-exec network wall and no
  filesystem confinement; Windows gets nothing below Python. Both say so in
  the log.

  CONVERTERS that set `SandboxPolicy.confine` get the SAME two mechanisms,
  narrowed to the paths that op declares -- its input file, its output
  directory, its model cache -- and, on the native lane, spawned as a
  launcher that installs the fence and `execv`s into ffmpeg/pdftoppm. They
  process attacker-supplied files through runtimes with long CVE histories
  and they need almost nothing, which is the whole reason it is worth it. On
  a kernel that cannot enforce, a converter runs UNCONFINED with a warning
  rather than refusing; `fence.warn_confinement_unavailable` says why.

  EVERY OTHER CALLER (`run_sandboxed`, `run_sandboxed_stdout_lines`,
  `open_sandboxed_process` without a `confine` -- the transcription workers
  and the workbench plugin subprocesses) gets process supervision, rlimits
  and a scrubbed environment, and NO kernel fence. Those run repo-owned code
  that needs the filesystem and, in the plugin case, declares
  `allow_network=True`.

Enforced for everything:
- op code runs in a child process with a wall-clock timeout on every
  platform (`SandboxPolicy.wall_seconds`, the load-bearing limit); CPU and
  memory rlimits (`cpu_seconds`/`memory_mb`) are POSIX-only and are not
  enforced on Windows
- the owned process tree is torn down together (`ProcessTreeController`), so
  a wall timeout or a cancel cannot leave a live descendant behind
- the child's environment is scrubbed (`_scrubbed_env` / `SCRUB_PREFIXES`):
  no provider keys reach it, only FRISKET_* vars and a small always-kept set,
  and HOME/TEMP/TMP are remapped to a child-owned scratch directory that no
  `env_passthrough` entry can override
- on macOS with `sandbox-exec` present, `allow_network=False` is a real
  OS-level network wall for the child and its subprocesses
  (`_darwin_netwall_argv`)

Additionally enforced for CODE RECIPES on Linux -- pinned by
tests/engine/test_sandbox_recipe_fence.py:
- the network. A seccomp-BPF filter refuses `socket()` for every address
  family except `AF_UNIX` (and refuses that too unless the run has a key
  broker to talk to), so `ctypes.CDLL("libc.so.6")` reaches no further than
  the stdlib does. `PYTHON_NETWALL_BOOTSTRAP`'s audit hook stays on top of it
  as defense in depth.
- new programs and new processes, below Python: the same filter refuses
  `execve`/`execveat` outright, and refuses `clone` unless `CLONE_THREAD` is
  set -- so threads still work and `ctypes`' `fork()` does not, which is what
  makes the audit hook's `os.fork` refusal real and takes away the fork bomb.
- the filesystem, via Landlock: read+execute on the interpreter, the stdlib,
  site-packages and the system library roots (plus six named files under
  /etc); read+write beneath the child's own scratch directory; nothing else.
  `~/.frisket/secrets/master.key` is refused, as is every project data file,
  the repository root and the rest of the user's home.
- reads of the server process's memory (`ptrace`, `process_vm_readv`,
  `/proc/<ppid>/*`), and the two ways out from under the above (`io_uring_*`,
  `open_by_handle_at`).
If the kernel cannot install all of that -- or cannot prove it afterwards --
the recipe REFUSES with `fence.SandboxEnforcementUnavailable` and no recipe
code runs.

Additionally enforced for a caller that sets `SandboxPolicy.confine`, on
Linux -- pinned by tests/engine/test_sandbox_media_fence.py:
- the same filter and the same ruleset, with the paths that op named. A
  converter reads its one input and writes its one output directory; anything
  else -- the operator's other documents, `~/.frisket/secrets/master.key`, the
  project store -- is refused by the kernel, and the network with it.
- one deliberate widening on the native lane: `execve` is permitted, because
  the fence is installed by a Python launcher that must then become
  ffmpeg/pdftoppm. Landlock still governs WHICH files carry EXECUTE, and
  whatever is exec'd inherits this same filter and ruleset.

NOT enforced:
- the filesystem, for a caller with no `confine`, and for code recipes on
  macOS and Windows. `cwd` is a scratch directory, but the child can read and
  write any path the server user can, by absolute path -- including
  `~/.frisket/secrets/master.key`, the key `team/secret_box.py` uses to
  decrypt every stored provider credential.
- the network, for a caller with neither `run_python_op` nor `confine`, off
  Darwin. `PYTHON_NETWALL_BOOTSTRAP` installs a CPython audit hook that denies
  the stdlib `socket.*` and `subprocess`/`os.system`/`os.posix_spawn`/
  `os.exec`/`os.fork` events. On its own that is a Python-level wall on two
  surfaces and nothing below them: code that calls libc directly never raises
  one of those audit events. Only a fenced child has a kernel filter under it.
- where a BROKERED recipe's AF_UNIX socket may connect. That is the audit
  hook alone -- seccomp cannot read `connect`'s sockaddr pointer and Landlock
  ABI 4 does not govern unix-socket connects -- so a `ctypes` call reaches any
  unix socket this user can. No production caller passes a broker to a code
  recipe, and a closure test keeps it that way; `run_python_op` logs a warning
  whenever one is passed.
- source readability. Everything on `sys.path` is granted read+execute,
  because that is what "can import" means: site-packages always, and the
  project's own `src/` tree on an editable install. Recipe code can read
  Frisket's source. It cannot read Frisket's data or keys.
- file *metadata*, even for code recipes. Landlock does not govern `stat`,
  `access` or `readlink`, so a fenced recipe can still learn that a path
  exists. It cannot read, write, list or execute it.
- key confinement beyond the environment. The key-broker endpoint
  (broker.py) -- a Unix socket on POSIX, an authenticated loopback TCP socket
  on Windows -- is how well-behaved op code SHOULD request completions
  without holding a key. It is a convenience, not a confinement: it does not
  stop code that reads a key off disk instead -- which, for a fenced recipe,
  Landlock now does.

So: a code recipe on Linux is confined by the kernel, and so is every
converter that declares a `Confinement`. What is left unfenced in this module
is safe for code the operator trusts and is not a containment story for code
they do not; hosted tiers swap those for gVisor/microVM execution behind
the same interface. On a self-hosted TEAM server, anyone who can author a
non-recipe action still has the server user's privileges -- the UI says so at
the code editor (web/src/components/action-panel/ActionParams.tsx).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit

from . import fence

logger = logging.getLogger(__name__)


@dataclass
class SandboxPolicy:
    cpu_seconds: int = 60
    memory_mb: int = 1024
    wall_seconds: int = 300
    allow_network: bool = False  # subprocess tools that fetch must declare it
    env_passthrough: list[str] = field(default_factory=list)  # explicit extras
    allowed_extra_env: list[str] = field(default_factory=list)
    # Narrow capability for repo-owned, static Python ``-c`` workers.  This is
    # not a general external-code sandbox: commands that do not resolve to the
    # current interpreter with exactly one ``-c`` body fail closed.
    trusted_python_netwall: bool = False
    # The kernel fence, with this op's declared read/write/exec needs. Set it
    # and the child gets the same seccomp filter and Landlock ruleset a code
    # recipe gets, narrowed to the paths named here. Unset (every other
    # caller) is the pre-fence perimeter: supervision, rlimits, scrubbed env.
    confine: fence.Confinement | None = None


@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    # Cooperative cancellation: the run's ``should_cancel`` observed a cancel
    # (or an outer cancel was requested) before the worker finished, so its
    # owned process tree was torn down. Distinct from ``timed_out`` — a caller
    # (the transcribe seam, the MapRunner fence) treats this as cancellation,
    # not a wall-timeout row failure.
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled


class SandboxTeardownError(RuntimeError):
    """The sandbox could not prove that an owned process tree exited.

    This is an infrastructure failure, not a worker/protocol error.  Callers
    must not report a clean close or start a replacement heavyweight worker
    after this exception without first recovering the owning Frisket process.
    """

    # A runner that durably terminalizes before re-raising binds the affected
    # run here. Lower sandbox layers leave it unset.
    run_id: int | None = None


class SandboxProcessCancelledError(Exception):
    """A ``should_cancel`` callback stopped an interactive sandbox process.

    Kept distinct from :class:`asyncio.CancelledError`: the former is an
    operator/run cancellation that an adapter translates at its recipe seam;
    the latter is cancellation of the parent asyncio task and propagates
    unchanged after process-tree cleanup.
    """


# Synthetic return code for a wall-timeout kill. Not a POSIX negative signal
# status: Windows-created timeout results must not pretend to be one.
SANDBOX_TIMEOUT_RETURN_CODE = 124

# Cosmetic placeholder return code for a cancelled result. Callers MUST branch
# on ``SandboxResult.cancelled``, never on this number: no numeric
# cancellation exit code is guaranteed. Kept distinct from the timeout code
# only so a stray log line is not misread as a timeout.
_CANCELLED_RETURN_CODE = 125

# Cancellation polling cadence and the bounded teardown escalation windows.
# The poll interval is intentionally coarse: ``should_cancel`` reads SQLite in
# queued runs, so this must not become a high-rate database loop. The grace
# windows keep an inherited pipe or a signal-ignoring child from stretching a
# cancellation out to the original worker wall limit.
_CANCEL_POLL_SECONDS = 0.2
# TERM is a bounded courtesy before KILL. A force-cancelled sandbox worker
# should stop promptly, and Frisket's own workers install no SIGTERM handler, so
# they exit on TERM within a poll and never consume this grace — it only bounds
# how long a signal-ignoring child delays escalation.
_TERM_GRACE_SECONDS = 1.0
_KILL_GRACE_SECONDS = 2.0
# Cadence for polling whether the owned tree has fully exited. Kept off
# ``proc.wait()`` on purpose: asyncio resolves ``proc.wait()`` only once the
# child's stdout/stderr pipes disconnect, so a descendant that inherited those
# pipes would gate escalation behind its OWN exit — the exact hazard we must
# avoid. We instead poll process-group / job liveness directly.
_TREE_POLL_SECONDS = 0.05

# Escalation signals. SIGKILL does not exist on Windows (and is ignored there
# anyway — the tree is torn down with taskkill), so fall back to a signal that
# does so importing/using this module never raises on native Windows.
_TERM_SIGNAL = signal.SIGTERM
_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)


class _Cancelled(SandboxProcessCancelledError):
    """Internal signal: ``should_cancel`` asked to stop. Never leaves this
    module — the one-shot entry points translate it into a cancelled
    ``SandboxResult`` and the interactive entry point translates it into its
    public base class."""


def _process_group_kwargs() -> dict:
    """Subprocess kwargs that make the child lead its own tree so the whole
    tree can be terminated together.

    POSIX: ``start_new_session=True`` runs ``setsid`` in the child, making it a
    process-group leader (pgid == pid). Its descendants — including the real
    worker running under the macOS ``sandbox-exec`` wrapper — inherit the group,
    so one ``os.killpg`` reaches all of them. POSIX resource limits are applied
    by the exec wrapper after this isolated child starts; no ``preexec_fn`` is
    used because it is unsafe when the parent has threads.

    Windows: ``CREATE_NEW_PROCESS_GROUP`` isolates the child from our console
    group; the tree itself is owned and torn down through a kill-on-close Job
    Object (see ``ProcessTreeController``), not by pid.
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _assign_windows_job(
    proc: asyncio.subprocess.Process,
    *,
    memory_limit_bytes: int | None = None,
    cpu_limit_seconds: int | None = None,
):
    """Create a kill-on-close Job Object and assign the freshly-spawned child to
    it, so terminating (or closing) the job destroys the WHOLE tree even if the
    root process exits first — Windows' ``taskkill /T`` walks the live
    parent->child links and cannot find double-forked orphans.

    Pure ctypes against kernel32 — no new dependency, and imported lazily so
    POSIX never touches this plumbing. Returns an opaque job handle, or None if
    any step failed — the caller (``ProcessTreeController``) then FAILS CLOSED rather
    than running an unownable tree."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    JobObjectExtendedLimitInformation = 9
    JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
    JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100

    class _BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
            ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC),
            ("IoInfo", _IO),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _EXTENDED()
    limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if memory_limit_bytes is not None:
        if memory_limit_bytes <= 0:
            kernel32.CloseHandle(job)
            return None
        info.JobMemoryLimit = int(memory_limit_bytes)
        limit_flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
    if cpu_limit_seconds is not None:
        if cpu_limit_seconds <= 0:
            kernel32.CloseHandle(job)
            return None
        # Windows job CPU times are expressed in 100-nanosecond units.  A job
        # limit covers the aggregate adapter tree rather than only its leader.
        info.BasicLimitInformation.PerJobUserTimeLimit = int(
            cpu_limit_seconds * 10_000_000
        )
        limit_flags |= JOB_OBJECT_LIMIT_JOB_TIME
    info.BasicLimitInformation.LimitFlags = limit_flags
    if not kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    ):
        kernel32.CloseHandle(job)
        return None
    handle = kernel32.OpenProcess(
        PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, proc.pid
    )
    if not handle:
        kernel32.CloseHandle(job)
        return None
    assigned = kernel32.AssignProcessToJobObject(job, handle)
    kernel32.CloseHandle(handle)
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return job


def _terminate_windows_job(job) -> bool:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        # Default restype (c_int) reads the BOOL result fine; nonzero == success.
        if not kernel32.TerminateJobObject(job, 1):
            logger.warning(
                "windows_job_terminate_failed",
                extra={
                    "event": "windows_job_terminate_failed",
                    "last_error": ctypes.get_last_error(),
                },
            )
            return False
    except OSError:
        logger.warning(
            "windows_job_terminate_error",
            exc_info=True,
            extra={"event": "windows_job_terminate_error"},
        )
        return False
    return True


def _job_active_processes(job) -> int | None:
    """Live process count in the Job via QueryInformationJobObject
    (JobObjectBasicAccountingInformation). Returns ActiveProcesses, or None if
    the query failed. Only ``ActiveProcesses`` is read."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class _ACCT(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", wintypes.LARGE_INTEGER),
            ("TotalKernelTime", wintypes.LARGE_INTEGER),
            ("ThisPeriodTotalUserTime", wintypes.LARGE_INTEGER),
            ("ThisPeriodTotalKernelTime", wintypes.LARGE_INTEGER),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    JobObjectBasicAccountingInformation = 1
    info = _ACCT()
    returned = wintypes.DWORD(0)
    ok = kernel32.QueryInformationJobObject(
        job,
        JobObjectBasicAccountingInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
        ctypes.byref(returned),
    )
    if not ok:
        return None
    return int(info.ActiveProcesses)


def _close_windows_job(job) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    with contextlib.suppress(Exception):
        kernel32.CloseHandle(job)


class ProcessTreeController:
    """Retains ownership of a launched child's whole process tree so cleanup
    stays correct even when the root process exits before its descendants.

    Public seam: also used outside this module by the media metadata adapter
    runner (media_metadata), which supervises a synchronous ``Popen`` —
    the controller only relies on ``proc.pid``/``proc.kill()``, so both
    process types are supported.

    POSIX: the process-group id equals the child pid BY CONSTRUCTION
    (``start_new_session=True``), so the group is signalled directly with
    ``os.killpg(pid, sig)`` — never via ``os.getpgid`` on a possibly-dead
    leader. A POSIX process group lives as long as ANY member does, so the
    group signal reaches a double-forked or TERM-ignoring grandchild that
    outlived its parent; ``ProcessLookupError`` means the group is already
    empty (success).

    Windows: a kill-on-close Job Object assigned at spawn. If ownership cannot
    be established the constructor FAILS CLOSED (kills the direct child and
    raises) rather than run an unownable tree.

    Ownership-race note: a child can spawn descendants between CreateProcess and
    AssignProcessToJobObject, and asyncio gives no CREATE_SUSPENDED. The
    containment is ordering: both entry points build the controller (and thus
    complete job assignment) BEFORE creating the task that writes the child's
    stdin, and the shipped sandbox workers do not spawn until they have read
    their stdin payload — so no descendant exists before assignment for any real
    caller."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process | subprocess.Popen[bytes],
        *,
        memory_limit_bytes: int | None = None,
        cpu_limit_seconds: int | None = None,
    ) -> None:
        self.proc = proc
        self._job = None
        if os.name == "nt":
            job = _assign_windows_job(
                proc,
                memory_limit_bytes=memory_limit_bytes,
                cpu_limit_seconds=cpu_limit_seconds,
            )
            if job is None:
                # Fail closed: an unownable tree must not run. Kill the direct
                # child best-effort and abort the operation.
                with contextlib.suppress(Exception):
                    proc.kill()
                raise RuntimeError("sandbox tree ownership unavailable")
            self._job = job

    def signal_tree(self, sig: int) -> None:
        """Signal the whole owned tree, synchronously. Once this returns the
        tree has been signalled even if a later ``await`` is interrupted, which
        is what makes teardown correct under outer cancellation."""
        pid = self.proc.pid
        if pid is None:
            return
        if os.name == "nt":
            # Ownership is guaranteed by the fail-closed constructor: terminate
            # the Job (no ``taskkill`` fail-open path).
            if self._job is not None:
                _terminate_windows_job(self._job)
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, sig)

    def tree_gone(self) -> bool:
        """Is the whole owned tree gone? POSIX tests the PROCESS GROUP, not the
        leader: ``killpg(pgid, 0)`` raising ``ProcessLookupError`` means the
        group is empty (every descendant exited), which stays correct after the
        leader itself dies. Windows queries the Job's live ActiveProcesses
        count (0 == tree gone). A failed query is not proof and therefore
        fails closed."""
        pid = self.proc.pid
        if pid is None:
            return True
        if os.name == "nt":
            if self._job is not None:
                active = _job_active_processes(self._job)
                if active is not None:
                    return active == 0
            return False
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    async def wait_tree_gone(self, timeout: float) -> bool:
        """Poll ``tree_gone`` up to ``timeout``. Returns True as soon as the
        tree is empty; False if the bound elapses first. Polling group liveness
        (not ``proc.wait()``) is what keeps teardown bounded even when a
        descendant inherited the child's pipes (invariant 6)."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            if self.tree_gone():
                return True
            if loop.time() >= deadline:
                return False
            await asyncio.sleep(_TREE_POLL_SECONDS)

    def close(self) -> None:
        if self._job is not None:
            _close_windows_job(self._job)
            self._job = None


async def _terminate_and_reap(
    controller: ProcessTreeController,
    *,
    pipe_tasks: tuple[asyncio.Future, ...] = (),
    reap_task: asyncio.Future | None = None,
) -> None:
    """Shared teardown for both entry points (invariant 2): terminate the owned
    process tree, then settle pipe tasks. Escalation is timed on GROUP/JOB
    liveness, never on ``proc.wait()`` — a descendant that inherited the child's
    pipes would otherwise gate KILL behind its own exit.
    TERM is a bounded courtesy; if the tree is not gone within the grace we KILL
    the whole group/job — reaching double-forked orphans and TERM-ignoring
    children even once the leader has exited — and wait, bounded, again."""
    tree_gone = False
    try:
        controller.signal_tree(_TERM_SIGNAL)
        tree_gone = await controller.wait_tree_gone(_TERM_GRACE_SECONDS)
        if not tree_gone:
            controller.signal_tree(_KILL_SIGNAL)
            tree_gone = await controller.wait_tree_gone(_KILL_GRACE_SECONDS)
    finally:
        # Once the tree is gone, give EOF a bounded chance to propagate through
        # the pipe readers.  Cancelling them immediately leaves Proactor pipe
        # transports alive until __del__ on Windows, after the event loop has
        # already closed.  A failed tree proof cannot promise EOF, so it keeps
        # the immediate-cancel path.
        tasks = tuple(dict.fromkeys(pipe_tasks))
        pending = set(tasks)
        if tree_gone and pending:
            _, pending = await asyncio.wait(pending, timeout=_KILL_GRACE_SECONDS)
        for task in pending:
            if not task.done():
                task.cancel()
        # Retrieve every result/exception and let cancelled tasks run their
        # cleanup before the caller is allowed to close the event loop.
        for task in tasks:
            with contextlib.suppress(BaseException):
                await task
    if not tree_gone:
        raise SandboxTeardownError(
            "sandbox process tree remained live after TERM/KILL escalation"
        )
    if reap_task is not None:
        try:
            await asyncio.wait_for(
                asyncio.shield(reap_task), timeout=_KILL_GRACE_SECONDS
            )
        except TimeoutError as exc:
            raise SandboxTeardownError(
                "sandbox direct child was not reaped after process-tree exit"
            ) from exc
        # Retrieve a completed task's exception rather than letting it become
        # an unobserved background failure.
        reap_task.result()


async def _run_teardown(
    controller: ProcessTreeController,
    pipe_tasks: tuple[asyncio.Future, ...] = (),
    *,
    reap_task: asyncio.Future | None = None,
) -> bool:
    """Run bounded teardown in an ISOLATED task and await it under
    ``asyncio.shield``. A fresh ``task.cancel()`` arriving while
    cleanup is in flight interrupts our await but NOT the shielded teardown, so
    the tree is still fully torn down; we record that a cancellation arrived and
    keep awaiting until teardown completes (it is internally bounded, so this
    terminates). Returns True if any outer cancellation arrived — the caller
    must then raise ``CancelledError`` rather than return a normal result, so a
    caller's cancellation is never erased into a ``SandboxResult``."""
    teardown = asyncio.ensure_future(
        _terminate_and_reap(controller, pipe_tasks=pipe_tasks, reap_task=reap_task)
    )
    outer_cancelled = False
    while not teardown.done():
        try:
            await asyncio.shield(teardown)
        except asyncio.CancelledError:
            outer_cancelled = True
    # Merely observing ``done()`` does not retrieve exceptions. In particular,
    # never discard SandboxTeardownError: a caller must not mistake an
    # unverified tree for a successfully cancelled worker.
    teardown.result()
    return outer_cancelled


async def _supervise(
    task: asyncio.Future,
    *,
    should_cancel: Callable[[], bool] | None,
    wall_seconds: int,
):
    """Await ``task`` while polling ``should_cancel`` and enforcing the wall
    deadline. Returns the task's result; raises ``_Cancelled`` on a cooperative
    cancel, ``TimeoutError`` at the deadline. A ``should_cancel`` that itself
    raises, an outer ``asyncio.CancelledError``, and a task that raises (e.g. a
    streaming line callback) all propagate for the caller to tear down and
    re-raise (invariants 4, 5). ``task`` is left running for teardown to cancel;
    ``asyncio.wait`` never cancels it on timeout."""
    loop = asyncio.get_event_loop()
    deadline = (loop.time() + wall_seconds) if wall_seconds else None
    while True:
        if should_cancel is not None and should_cancel():
            raise _Cancelled
        timeout = _CANCEL_POLL_SECONDS if should_cancel is not None else None
        if deadline is not None:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            timeout = remaining if timeout is None else min(timeout, remaining)
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if task in done:
            return task.result()


async def _supervise_with_teardown(
    task: asyncio.Future,
    *,
    controller: ProcessTreeController,
    pipe_tasks: tuple[asyncio.Future, ...],
    should_cancel: Callable[[], bool] | None,
    wall_seconds: int,
) -> tuple[Any, SandboxResult | None]:
    """Await ``task`` under ``_supervise`` and run the shared teardown
    choreography on every failure path — identical for buffered and streaming
    execution. Returns ``(result, None)`` on success or
    ``(None, SandboxResult)`` when a cooperative cancel or wall timeout was
    converted into a terminal result. The except order is load-bearing:
    ``_Cancelled`` (a cooperative cancel observed by ``_supervise``) and
    ``TimeoutError`` become results; every other ``BaseException`` — an outer
    ``asyncio.CancelledError`` (invariant 4), a raising ``should_cancel`` or
    streaming line callback (invariant 5) — tears down the owned tree and
    re-raises, except that a FRESH cancellation arriving during teardown of a
    non-cancel error surfaces as cancellation rather than the error, so a
    caller's cancel is never erased."""
    try:
        result = await _supervise(
            task,
            should_cancel=should_cancel,
            wall_seconds=wall_seconds,
        )
    except _Cancelled:
        if await _run_teardown(controller, pipe_tasks):
            raise asyncio.CancelledError from None
        return None, SandboxResult(
            returncode=_CANCELLED_RETURN_CODE,
            stdout="",
            stderr="cancelled",
            cancelled=True,
        )
    except TimeoutError:
        if await _run_teardown(controller, pipe_tasks):
            raise asyncio.CancelledError from None
        return None, SandboxResult(
            returncode=SANDBOX_TIMEOUT_RETURN_CODE,
            stdout="",
            stderr="wall timeout",
            timed_out=True,
        )
    except BaseException as exc:
        cancelled_during = await _run_teardown(controller, pipe_tasks)
        if not isinstance(exc, asyncio.CancelledError) and cancelled_during:
            raise asyncio.CancelledError() from exc
        raise
    return result, None


SCRUB_PREFIXES = (
    "ANTHROPIC",
    "OPENAI",
    "GEMINI",
    "OPENROUTER",
    "STRIPE",
    "HETZNER",
    "CLOUDFLARE",
    "RESEND",
    "NPM",
    "SENTRY",
    "TOGETHER",
    "GOOGLE",
    "AWS",
    "API",
    "SECRET",
    "TOKEN",
    "KEY",
)


PYTHON_NETWALL_BOOTSTRAP = r"""
import os as _frisket_netwall_os
import sys as _frisket_netwall_sys
from urllib.parse import unquote as _frisket_netwall_unquote
from urllib.parse import urlsplit as _frisket_netwall_urlsplit

_FRISKET_NETWALL_SOCKET_EVENTS = {
    "socket.bind",
    "socket.connect",
    "socket.getaddrinfo",
    "socket.gethostbyaddr",
    "socket.gethostbyname",
    "socket.getnameinfo",
    "socket.sendmsg",
    "socket.sendto",
}

# Parse the broker endpoint once. The token is authentication; the exact
# endpoint (a Unix path or a literal (host, port) tuple) is the network
# allowlist -- every other socket-audit event stays denied.
_frisket_netwall_endpoint = _frisket_netwall_os.environ.get("FRISKET_BROKER_ENDPOINT")
_frisket_netwall_allowed_unix = None
_frisket_netwall_allowed_tcp = None
if _frisket_netwall_endpoint:
    _frisket_netwall_parts = _frisket_netwall_urlsplit(_frisket_netwall_endpoint)
    if _frisket_netwall_parts.scheme == "unix":
        _frisket_netwall_allowed_unix = _frisket_netwall_unquote(
            _frisket_netwall_parts.path
        )
    elif (
        _frisket_netwall_parts.scheme == "tcp"
        and _frisket_netwall_parts.hostname == "127.0.0.1"
        and _frisket_netwall_parts.port is not None
    ):
        # Pinned to the literal loopback address only -- a tcp:// endpoint
        # naming any other host (numeric or not) leaves
        # _frisket_netwall_allowed_tcp unset below, so socket.connect stays
        # denied for it (fail closed).
        _frisket_netwall_allowed_tcp = ("127.0.0.1", _frisket_netwall_parts.port)

def _frisket_netwall_audit(
    event, args, socket_events=_FRISKET_NETWALL_SOCKET_EVENTS
):
    if event == "socket.connect":
        address = args[1] if len(args) > 1 else None
        if (
            _frisket_netwall_allowed_unix is not None
            and isinstance(address, str)
            and address == _frisket_netwall_allowed_unix
        ):
            return
        if (
            _frisket_netwall_allowed_tcp is not None
            and isinstance(address, tuple)
            and address == _frisket_netwall_allowed_tcp
        ):
            return
    if event in socket_events:
        raise PermissionError("network access is disabled by the Frisket sandbox")
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.fork"}:
        raise PermissionError("subprocesses are disabled by the Frisket sandbox netwall")

_frisket_netwall_sys.addaudithook(_frisket_netwall_audit)
del _frisket_netwall_audit, _frisket_netwall_sys, _FRISKET_NETWALL_SOCKET_EVENTS
"""


_ALWAYS_KEPT_ENV_KEYS = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "TEMP",
    "TMP",
    "LANG",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
)


def _scrubbed_env(
    policy: SandboxPolicy, extra: dict[str, str], *, default_home: Path
) -> dict[str, str]:
    home = str(default_home)
    base = {
        # os.defpath, not a hardcoded POSIX path: portable fallback search
        # path on both POSIX (os.pathsep-joined) and Windows.
        "PATH": os.defpath,
        "HOME": home,
        "USERPROFILE": home,
        "TEMP": home,
        "TMP": home,
        "LANG": "C.UTF-8",
    }
    if os.name == "nt":
        # Windows subprocess creation needs a valid SystemRoot for
        # side-by-side assemblies; COMSPEC/PATHEXT are likewise required by
        # ordinary Windows child-process resolution.
        for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"):
            if k in os.environ:
                base[k] = os.environ[k]
    for k in policy.env_passthrough:
        if k in os.environ:
            base[k] = os.environ[k]
    allowed_extra = set(policy.allowed_extra_env)
    for k, v in extra.items():
        if not k.startswith("FRISKET_") and k not in allowed_extra:
            raise ValueError(f"sandbox extra env is not allowlisted: {k}")
        base[k] = v
    for k in list(base):
        upper = k.upper()
        if k in allowed_extra:
            continue
        if k not in _ALWAYS_KEPT_ENV_KEYS and any(p in upper for p in SCRUB_PREFIXES):
            del base[k]
    # The child-owned scratch home is a security/isolation invariant, not a
    # default: neither an env_passthrough entry (e.g. a caller passing
    # through ambient HOME) nor an extra_env value may override it. Re-assert
    # all four after every merge above, regardless of what ran.
    base["HOME"] = home
    base["USERPROFILE"] = home
    base["TEMP"] = home
    base["TMP"] = home
    return base


def _limited_exec_argv(argv: list[str], policy: SandboxPolicy) -> list[str]:
    """Wrap a POSIX command so its child-owned entry point applies rlimits.

    A Python ``preexec_fn`` would run after fork in a possibly threaded parent,
    where it can deadlock before ``Popen`` returns.  Executing this small helper
    has the same limit-before-target-exec ordering without parent-thread state.
    Windows uses its Job Object limits instead.
    """
    if os.name == "nt":
        return argv
    payload = json.dumps([policy.cpu_seconds, policy.memory_mb, argv])
    return [
        str(Path(sys.executable).resolve()),
        "-I",
        str(Path(__file__).with_name("_exec_with_limits.py").resolve()),
        payload,
    ]


def _sbpl_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _darwin_netwall_argv(
    argv: list[str], scratch: Path, policy: SandboxPolicy, env: dict[str, str]
) -> list[str]:
    """Wrap a command in macOS seatbelt when network is not allowed.

    The profile intentionally allows default local process/filesystem behavior
    and denies only network operations. This keeps existing sandboxed media
    tools usable while turning `allow_network=False` into a real OS-level wall
    for the child and its subprocesses on local Darwin development hosts.
    """
    if policy.allow_network or sys.platform != "darwin":
        return argv
    sandbox_exec = shutil.which("sandbox-exec")
    if not sandbox_exec:
        return argv
    profile = scratch / ".frisket-netwall.sb"
    lines = [
        "(version 1)",
        "(allow default)",
        "(deny network-outbound)",
        "(deny network-inbound)",
    ]
    broker_endpoint = env.get("FRISKET_BROKER_ENDPOINT")
    if broker_endpoint:
        parts = urlsplit(broker_endpoint)
        if parts.scheme == "unix":
            # Extract the path only for a Unix endpoint; never treat the
            # query token as a path or write it to the Seatbelt profile.
            broker_path = _sbpl_string(os.path.realpath(unquote(parts.path)))
            lines.append(
                f'(allow network-outbound (remote unix-socket (path-literal "{broker_path}")))'
            )
        # A tcp:// (Windows loopback) endpoint has no macOS Seatbelt path
        # rule; that transport only exists off-Darwin, and its
        # authentication is the per-broker token, not this OS network wall.
    profile.write_text("\n".join(lines) + "\n")
    return [sandbox_exec, "-f", str(profile), *argv]


def _has_process_netwall(policy: SandboxPolicy) -> bool:
    return (
        not policy.allow_network
        and sys.platform == "darwin"
        and shutil.which("sandbox-exec") is not None
    )


def _trusted_python_netwall_argv(argv: list[str], policy: SandboxPolicy) -> list[str]:
    """Apply the shared audit netwall to one explicitly trusted Python body."""
    if not policy.trusted_python_netwall or policy.allow_network:
        return argv
    is_current_python = (
        bool(argv) and Path(argv[0]).resolve() == Path(sys.executable).resolve()
    )
    if len(argv) != 3 or not is_current_python or argv[1] != "-c":
        raise ValueError(
            "trusted Python netwall requires the current interpreter and one -c body"
        )
    if sys.platform == "darwin":
        if not _has_process_netwall(policy):
            raise RuntimeError("trusted Python netwall requires sandbox-exec on macOS")
        return argv
    return [argv[0], "-c", f"{PYTHON_NETWALL_BOOTSTRAP}\n{argv[2]}"]


def _fenced_argv(argv: list[str], policy: SandboxPolicy) -> list[str]:
    """The argv actually spawned: netwall wrapper first, then the fence.

    Order matters and mirrors `run_python_op`: the kernel fence goes outermost
    so that everything after it -- including the audit hook that backs it up
    in Python -- runs already confined.

    Two child shapes, one mechanism:

      a Python worker (`<this interpreter> -c <body>`) gets the fence prelude
      prepended to its body, with `execve` refused exactly as a recipe's is;

      a native converter (ffmpeg, ffprobe, pdftoppm) is spawned as a Python
      launcher that installs the fence and `execv`s into the binary, keeping
      the pid the process supervisor already owns.

    Any other shape with a confinement set is a caller error and fails closed
    rather than running unfenced: a profile that quietly did nothing is the
    silent degradation this whole file exists to remove.
    """
    argv = _trusted_python_netwall_argv(argv, policy)
    profile = policy.confine
    if profile is None:
        return argv
    if not argv:
        raise ValueError("a confined sandbox child needs a command")
    unavailable = fence.kernel_can_confine()
    if unavailable is not None:
        fence.warn_confinement_unavailable(profile.op, unavailable)
        return argv
    if profile.exec_binary is not None:
        if Path(argv[0]).resolve() != Path(profile.exec_binary).resolve():
            raise ValueError(
                f"confinement declares exec_binary {profile.exec_binary!r} but "
                f"the command runs {argv[0]!r}"
            )
        return [
            sys.executable,
            "-I",
            "-c",
            fence.native_launcher_body(profile, argv),
        ]
    is_current_python = Path(argv[0]).resolve() == Path(sys.executable).resolve()
    if len(argv) != 3 or argv[1] != "-c" or not is_current_python:
        raise ValueError(
            "a confined Python child must be the current interpreter with one "
            "-c body; name the program in Confinement.exec_binary instead"
        )
    return [argv[0], "-c", fence.linux_confined_prelude(profile) + argv[2]]


def _raise_if_fence_refused(result: SandboxResult, policy: SandboxPolicy) -> None:
    """A confined child that could not put its own fence up is a hard failure.

    Reached only when `kernel_can_confine()` already said this kernel can:
    what is left is a profile this code got wrong (a path Landlock rejected, a
    syscall number wrong for the architecture), which is a bug and must be
    named rather than retried unconfined.
    """
    if policy.confine is None or result.ok:
        return
    refusal = fence.refusal_reason(result.stderr)
    if refusal is None:
        return
    raise fence.SandboxEnforcementUnavailable(
        f"refused to run {policy.confine.op}: this kernel reported it can "
        f"enforce the sandbox fence, but installing this op's profile failed "
        f"({refusal}). No op code ran."
    )


def _child_home(scratch: Path) -> Path:
    scratch.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".frisket-home-", dir=scratch))


_SANDBOX_PARENT_FRAME_MAX_BYTES = 1 * 1024 * 1024
_SANDBOX_CHILD_FRAME_MAX_BYTES = 64 * 1024 * 1024
_SANDBOX_STDERR_TAIL_MAX_BYTES = 64 * 1024
_SANDBOX_PIPE_CHUNK_BYTES = 8 * 1024
_SANDBOX_STDIN_CHUNK_BYTES = 64 * 1024


def _checked_wall_seconds(wall_seconds: float) -> float:
    try:
        value = float(wall_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "sandbox process wall_seconds must be finite and positive"
        ) from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError("sandbox process wall_seconds must be finite and positive")
    return value


def _checked_response_limit(response_limit: int) -> int:
    if (
        isinstance(response_limit, bool)
        or not isinstance(response_limit, int)
        or not 0 <= response_limit <= _SANDBOX_CHILD_FRAME_MAX_BYTES
    ):
        raise ValueError(
            "sandbox response frame limit must be between 0 and 67108864 bytes"
        )
    return response_limit


class SandboxedProcess:
    """One owned, interactive sandbox child with bounded framed byte I/O.

    This handle deliberately knows nothing about JSON, model names, rows, or
    retries. One caller frame is followed by exactly one child frame; the
    higher-level protocol adapter validates those frame bodies. Any interrupted
    exchange poisons and tears down the handle rather than attempting to
    resynchronise the byte stream.
    """

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        controller: ProcessTreeController,
        *,
        scratch: Path,
        child_home: Path,
        own_scratch: bool,
        should_cancel: Callable[[], bool] | None,
    ) -> None:
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:
            raise RuntimeError("interactive sandbox process requires three pipes")
        self._proc = proc
        self._controller = controller
        self._scratch = scratch
        self._child_home = child_home
        self._own_scratch = own_scratch
        self._should_cancel = should_cancel
        self._state = "open"
        self._returncode: int | None = None
        self._teardown_error: SandboxTeardownError | None = None
        self._stderr_tail = bytearray()
        self._resources_cleaned = False
        self._io_task: asyncio.Future | None = None
        self._abort_task: asyncio.Future | None = None

        # Ownership is established before either task is created. The reap task
        # is retained for every exit path; the stderr task continuously prevents
        # diagnostics from filling the child pipe while retaining only a small
        # in-memory tail that is never exposed through public errors.
        self._reap_task: asyncio.Future = asyncio.ensure_future(proc.wait())
        self._stderr_task: asyncio.Future = asyncio.ensure_future(self._drain_stderr())

    @property
    def pid(self) -> int | None:
        return self._proc.pid

    @property
    def returncode(self) -> int | None:
        return self._returncode if self._state == "closed" else self._proc.returncode

    @property
    def closed(self) -> bool:
        return self._state == "closed"

    @property
    def stderr_tail_size(self) -> int:
        """Number of retained diagnostic bytes, without exposing their content."""

        return len(self._stderr_tail)

    async def __aenter__(self) -> SandboxedProcess:
        if self._state != "open":
            raise RuntimeError("sandbox process is not open")
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> bool:
        if self._state not in {"closed", "teardown_failed"}:
            await self.abort()
        elif self._teardown_error is not None:
            raise self._teardown_error
        return False

    async def _drain_stderr(self) -> None:
        assert self._proc.stderr is not None
        while True:
            chunk = await self._proc.stderr.read(_SANDBOX_PIPE_CHUNK_BYTES)
            if not chunk:
                return
            self._stderr_tail.extend(chunk)
            overflow = len(self._stderr_tail) - _SANDBOX_STDERR_TAIL_MAX_BYTES
            if overflow > 0:
                del self._stderr_tail[:overflow]

    async def _supervise_io(self, task: asyncio.Future, *, wall_seconds: float):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + wall_seconds
        watch_stderr = True
        while True:
            if self._should_cancel is not None and self._should_cancel():
                raise _Cancelled
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            timeout = remaining
            if self._should_cancel is not None:
                timeout = min(timeout, _CANCEL_POLL_SECONDS)
            watched = {task}
            if watch_stderr:
                watched.add(self._stderr_task)
            done, _ = await asyncio.wait(watched, timeout=timeout)
            if self._stderr_task in done:
                # A clean stderr EOF is allowed; an incremental-drain exception
                # is fatal and takes precedence over an otherwise-ready frame.
                self._stderr_task.result()
                watch_stderr = False
            if task in done:
                return task.result()

    @staticmethod
    def _validate_outbound_frame(
        payload: bytes, *, request_limit: int = _SANDBOX_PARENT_FRAME_MAX_BYTES
    ) -> bytes:
        if not isinstance(payload, bytes):
            raise TypeError("sandbox frame payload must be bytes")
        if type(request_limit) is not int or not 1 <= request_limit <= 64 * 1024 * 1024:
            raise ValueError(
                "sandbox parent frame limit must be between 1 and 67108864"
            )
        if len(payload) > request_limit:
            raise ValueError(f"sandbox parent frame exceeds {request_limit}-byte limit")
        return payload

    async def _write_frame(self, payload: bytes) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(len(payload).to_bytes(4, "big"))
        self._proc.stdin.write(payload)
        await self._proc.stdin.drain()

    async def _read_frame(self, *, response_limit: int) -> bytes:
        assert self._proc.stdout is not None
        try:
            prefix = await self._proc.stdout.readexactly(4)
        except asyncio.IncompleteReadError:
            raise EOFError("sandbox child ended inside a frame prefix") from None
        size = int.from_bytes(prefix, "big")
        if size > response_limit:
            raise ValueError("sandbox child frame exceeds configured response limit")
        try:
            return await self._proc.stdout.readexactly(size)
        except asyncio.IncompleteReadError:
            raise EOFError("sandbox child ended inside a frame payload") from None

    async def _exchange_frame_io(self, payload: bytes, *, response_limit: int) -> bytes:
        await self._write_frame(payload)
        return await self._read_frame(response_limit=response_limit)

    def _claim_io(self) -> None:
        if self._teardown_error is not None:
            raise self._teardown_error
        if self._state != "open":
            raise RuntimeError("sandbox process is not open")
        if self._io_task is not None:
            raise RuntimeError("sandbox process already has an in-flight exchange")

    async def exchange_frame(
        self,
        payload: bytes,
        *,
        wall_seconds: float,
        response_limit: int = _SANDBOX_CHILD_FRAME_MAX_BYTES,
        request_limit: int = _SANDBOX_PARENT_FRAME_MAX_BYTES,
    ) -> bytes:
        """Write one frame and read one frame under a single wall deadline."""

        payload = self._validate_outbound_frame(payload, request_limit=request_limit)
        checked_limit = _checked_response_limit(response_limit)
        checked_wall = _checked_wall_seconds(wall_seconds)
        self._claim_io()
        io_task: asyncio.Future = asyncio.ensure_future(
            self._exchange_frame_io(payload, response_limit=checked_limit)
        )
        self._io_task = io_task
        try:
            return await self._supervise_io(io_task, wall_seconds=checked_wall)
        except _Cancelled:
            self._state = "poisoned"
            try:
                await self.abort()
            except asyncio.CancelledError:
                raise
            raise SandboxProcessCancelledError("sandbox process cancelled") from None
        except BaseException as exc:
            self._state = "poisoned"
            try:
                await self.abort()
            except asyncio.CancelledError:
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
                raise asyncio.CancelledError() from exc
            raise
        finally:
            if self._io_task is io_task:
                self._io_task = None

    async def _graceful_close_io(self, close_frame: bytes) -> int:
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        await self._write_frame(close_frame)
        self._proc.stdin.close()
        await self._proc.stdin.wait_closed()
        extra = await self._proc.stdout.read(1)
        if extra:
            raise RuntimeError("sandbox child emitted output after close")
        # Cancelling the graceful-close task must not cancel the retained reap
        # proof; forced teardown will await this same task after killing the
        # tree.
        returncode = int(await asyncio.shield(self._reap_task))
        await self._stderr_task
        return returncode

    async def close(self, close_frame: bytes, *, wall_seconds: float) -> int:
        """Request clean child exit, reap it, and prove the owned tree empty."""

        close_frame = self._validate_outbound_frame(close_frame)
        checked_wall = _checked_wall_seconds(wall_seconds)
        if self._teardown_error is not None:
            raise self._teardown_error
        if self._state == "closed":
            assert self._returncode is not None
            return self._returncode
        self._claim_io()
        self._state = "closing"
        close_task: asyncio.Future = asyncio.ensure_future(
            self._graceful_close_io(close_frame)
        )
        self._io_task = close_task
        try:
            returncode = int(
                await self._supervise_io(close_task, wall_seconds=checked_wall)
            )
        except _Cancelled:
            self._state = "poisoned"
            try:
                await self.abort()
            except asyncio.CancelledError:
                raise
            raise SandboxProcessCancelledError("sandbox process cancelled") from None
        except BaseException as exc:
            self._state = "poisoned"
            try:
                await self.abort()
            except asyncio.CancelledError:
                if isinstance(exc, asyncio.CancelledError):
                    raise exc
                raise asyncio.CancelledError() from exc
            raise
        finally:
            if self._io_task is close_task:
                self._io_task = None

        self._returncode = returncode
        if not self._controller.tree_gone():
            # A clean direct-child exit is insufficient while an owned
            # descendant remains. Reuse the ordinary TERM/KILL escalation and
            # only return the direct child's code after proof succeeds.
            await self.abort()
            return returncode
        self._state = "closed"
        self._cleanup_resources()
        return returncode

    def _teardown_tasks(self) -> tuple[asyncio.Future, ...]:
        tasks = [self._stderr_task]
        if self._io_task is not None:
            tasks.append(self._io_task)
        return tuple(tasks)

    async def _abort_impl(self) -> None:
        try:
            await _run_teardown(
                self._controller,
                self._teardown_tasks(),
                reap_task=self._reap_task,
            )
        except SandboxTeardownError as exc:
            self._teardown_error = exc
            self._state = "teardown_failed"
            raise
        else:
            self._state = "closed"
            if self._proc.returncode is not None:
                self._returncode = int(self._proc.returncode)
            self._cleanup_resources()

    async def abort(self) -> None:
        """Force bounded TERM/KILL teardown and verify the owned tree is gone."""

        if self._teardown_error is not None:
            raise self._teardown_error
        if self._state == "closed":
            return
        if self._abort_task is None:
            self._state = "aborting"
            self._abort_task = asyncio.ensure_future(self._abort_impl())
        outer_cancelled = False
        while not self._abort_task.done():
            try:
                await asyncio.shield(self._abort_task)
            except asyncio.CancelledError:
                outer_cancelled = True
        # Infrastructure failure wins over cancellation: a caller must be able
        # to poison admission when tree death could not be proved.
        self._abort_task.result()
        if outer_cancelled:
            raise asyncio.CancelledError

    def _cleanup_resources(self) -> None:
        if self._resources_cleaned:
            return
        self._resources_cleaned = True
        self._controller.close()
        shutil.rmtree(self._child_home, ignore_errors=True)
        if self._own_scratch:
            shutil.rmtree(self._scratch, ignore_errors=True)


async def _settle_unowned_process(proc: asyncio.subprocess.Process) -> None:
    """Kill/reap a root that failed before process-tree ownership existed.

    No initialization byte has been written at this point, so the static worker
    bootstrap has not been allowed to spawn descendants.
    """

    async def settle() -> None:
        if proc.stdin is not None:
            proc.stdin.close()
        with contextlib.suppress(Exception):
            proc.kill()
        try:
            await asyncio.wait_for(proc.communicate(), timeout=_KILL_GRACE_SECONDS)
        except TimeoutError as exc:
            raise SandboxTeardownError(
                "sandbox root was not reaped after ownership setup failed"
            ) from exc
        except Exception as exc:
            if proc.returncode is None:
                raise SandboxTeardownError(
                    "sandbox root reap failed after ownership setup failed"
                ) from exc
        if proc.returncode is None:
            raise SandboxTeardownError(
                "sandbox root remained live after ownership setup failed"
            )

    task = asyncio.ensure_future(settle())
    outer_cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            outer_cancelled = True
    task.result()
    if outer_cancelled:
        raise asyncio.CancelledError


async def _spawn_owned_process(
    argv: list[str],
    *,
    scratch: Path,
    env: dict[str, str],
    stdin: object,
    policy: SandboxPolicy,
    stream_limit: int = 64 * 1024,
) -> tuple[asyncio.subprocess.Process, ProcessTreeController, bool]:
    """Spawn a root, establish tree ownership, and preserve outer cancellation.

    Process creation is shielded until it has either failed or returned a root:
    cancelling the caller in the OS-spawn window therefore cannot discard a
    successfully-created process before Frisket assigns its process group/Job.
    If ownership establishment fails, the still-unowned root is killed and
    reaped before the original failure (or a pending outer cancellation) is
    surfaced.  No caller may start its stdin I/O before this helper returns.

    The boolean records cancellation observed while spawn was settling.  A
    caller that receives ``True`` owns the returned tree and must tear it down
    before re-raising :class:`asyncio.CancelledError`.
    """

    spawn_task = asyncio.ensure_future(
        asyncio.create_subprocess_exec(
            *_limited_exec_argv(
                _darwin_netwall_argv(argv, scratch, policy, env), policy
            ),
            cwd=scratch,
            env=env,
            stdin=stdin,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=stream_limit,
            **_process_group_kwargs(),
        )
    )
    outer_cancelled = False
    while not spawn_task.done():
        try:
            await asyncio.shield(spawn_task)
        except asyncio.CancelledError:
            # Let process creation settle so a successfully-created child
            # cannot be lost between the OS spawn and ownership setup.
            outer_cancelled = True
    try:
        proc = spawn_task.result()
    except BaseException as exc:
        if outer_cancelled and not isinstance(exc, asyncio.CancelledError):
            raise asyncio.CancelledError() from exc
        raise
    try:
        controller = ProcessTreeController(proc)
    except BaseException as exc:
        try:
            await _settle_unowned_process(proc)
        except asyncio.CancelledError:
            outer_cancelled = True
        if outer_cancelled and not isinstance(exc, asyncio.CancelledError):
            raise asyncio.CancelledError() from exc
        raise
    return proc, controller, outer_cancelled


async def _teardown_cancelled_spawn(
    proc: asyncio.subprocess.Process, controller: ProcessTreeController
) -> None:
    """Tear down/reap a process whose caller was cancelled during spawn."""

    # ``communicate`` settles the pipe transports while the independent wait
    # task is retained as the direct-child reap proof.  Neither task can begin
    # feeding caller input: this path is entered before legacy entry points
    # create their normal communication task.
    communicate_task: asyncio.Future = asyncio.ensure_future(proc.communicate())
    reap_task: asyncio.Future = asyncio.ensure_future(proc.wait())
    await _run_teardown(controller, (communicate_task,), reap_task=reap_task)


async def _communicate_stdin_file(
    proc: asyncio.subprocess.Process, stdin_file: BinaryIO
) -> tuple[bytes, bytes]:
    """Feed a file only after ownership, while draining both output pipes."""

    if proc.stdin is None or proc.stdout is None or proc.stderr is None:
        raise RuntimeError("sandbox file communication requires three pipes")

    async def feed() -> None:
        try:
            while True:
                # ``stdin_file`` is a binary temporary/regular file at every
                # current call site. Chunking keeps the event-loop stall bounded
                # and, unlike passing the descriptor to CreateProcess, withholds
                # every initialization byte until tree ownership is established.
                chunk = stdin_file.read(_SANDBOX_STDIN_CHUNK_BYTES)
                if not chunk:
                    return
                if not isinstance(chunk, bytes):
                    raise TypeError("sandbox stdin_file must be opened in binary mode")
                proc.stdin.write(chunk)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            proc.stdin.close()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                await proc.stdin.wait_closed()

    feed_task: asyncio.Future = asyncio.ensure_future(feed())
    stdout_task: asyncio.Future = asyncio.ensure_future(proc.stdout.read())
    stderr_task: asyncio.Future = asyncio.ensure_future(proc.stderr.read())
    reap_task: asyncio.Future = asyncio.ensure_future(proc.wait())
    owned_tasks = (feed_task, stdout_task, stderr_task, reap_task)
    try:
        await asyncio.gather(*owned_tasks)
    except BaseException:
        for task in owned_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*owned_tasks, return_exceptions=True)
        raise
    return stdout_task.result(), stderr_task.result()


async def open_sandboxed_process(
    argv: list[str],
    *,
    policy: SandboxPolicy | None = None,
    extra_env: dict[str, str] | None = None,
    scratch_dir: str | Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> SandboxedProcess:
    """Spawn an owned interactive child without writing an initialization byte."""

    policy = policy or SandboxPolicy()
    if should_cancel is not None and should_cancel():
        raise SandboxProcessCancelledError("sandbox process cancelled before start")
    own_scratch = scratch_dir is None
    scratch = (
        Path(scratch_dir)
        if scratch_dir
        else Path(tempfile.mkdtemp(prefix="frisket-op-"))
    )
    child_home: Path | None = None
    proc: asyncio.subprocess.Process | None = None
    process: SandboxedProcess | None = None
    try:
        child_home = _child_home(scratch)
        env = _scrubbed_env(policy, extra_env or {}, default_home=child_home)
        child_argv = _fenced_argv(argv, policy)
        proc, controller, outer_cancelled = await _spawn_owned_process(
            child_argv,
            scratch=scratch,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            policy=policy,
        )
        process = SandboxedProcess(
            proc,
            controller,
            scratch=scratch,
            child_home=child_home,
            own_scratch=own_scratch,
            should_cancel=should_cancel,
        )
        if outer_cancelled:
            await process.abort()
            raise asyncio.CancelledError
        return process
    except BaseException:
        # A strict teardown failure deliberately retains the controller and
        # scratch resources: closing/removing them would abandon containment of
        # a tree that may still be live. Every pre-ownership failure and every
        # verified cleanup may release the local paths normally.
        if process is None or process._teardown_error is None:
            if child_home is not None:
                shutil.rmtree(child_home, ignore_errors=True)
            if own_scratch:
                shutil.rmtree(scratch, ignore_errors=True)
        raise


async def run_sandboxed(
    argv: list[str],
    *,
    policy: SandboxPolicy | None = None,
    stdin_data: bytes | None = None,
    stdin_file: BinaryIO | None = None,
    extra_env: dict[str, str] | None = None,
    scratch_dir: str | Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> SandboxResult:
    if stdin_data is not None and stdin_file is not None:
        raise ValueError("run_sandboxed accepts stdin_data or stdin_file, not both")
    policy = policy or SandboxPolicy()
    # A pre-observed cancellation creates no worker process.
    if should_cancel is not None and should_cancel():
        return SandboxResult(
            returncode=_CANCELLED_RETURN_CODE,
            stdout="",
            stderr="cancelled",
            cancelled=True,
        )
    own_scratch = scratch_dir is None
    scratch = (
        Path(scratch_dir)
        if scratch_dir
        else Path(tempfile.mkdtemp(prefix="frisket-op-"))
    )
    controller: ProcessTreeController | None = None
    child_home: Path | None = None
    try:
        child_home = _child_home(scratch)
        env = _scrubbed_env(policy, extra_env or {}, default_home=child_home)
        child_argv = _fenced_argv(argv, policy)
        if stdin_file is not None:
            with contextlib.suppress(AttributeError, OSError, ValueError):
                stdin_file.seek(0)
        stdin_source = (
            asyncio.subprocess.PIPE
            if stdin_file is not None or stdin_data is not None
            else asyncio.subprocess.DEVNULL
        )
        proc, controller, outer_cancelled = await _spawn_owned_process(
            child_argv,
            scratch=scratch,
            env=env,
            stdin=stdin_source,
            policy=policy,
        )
        if outer_cancelled:
            await _teardown_cancelled_spawn(proc, controller)
            raise asyncio.CancelledError
        if stdin_file is None:
            communication = proc.communicate(stdin_data)
        else:
            communication = _communicate_stdin_file(proc, stdin_file)
        comm_task: asyncio.Future = asyncio.ensure_future(communication)
        streams, early = await _supervise_with_teardown(
            comm_task,
            controller=controller,
            pipe_tasks=(comm_task,),
            should_cancel=should_cancel,
            wall_seconds=policy.wall_seconds,
        )
        if early is not None:
            return early
        stdout, stderr = streams
        result = SandboxResult(
            returncode=proc.returncode or 0,
            stdout=stdout.decode(errors="replace"),
            stderr=stderr.decode(errors="replace"),
        )
        _raise_if_fence_refused(result, policy)
        return result
    finally:
        if controller is not None:
            controller.close()
        if child_home is not None:
            shutil.rmtree(child_home, ignore_errors=True)
        if own_scratch:
            shutil.rmtree(scratch, ignore_errors=True)


async def run_sandboxed_stdout_lines(
    argv: list[str],
    *,
    on_stdout_line: Callable[[str], None],
    policy: SandboxPolicy | None = None,
    stdin_data: bytes | None = None,
    extra_env: dict[str, str] | None = None,
    scratch_dir: str | Path | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> SandboxResult:
    policy = policy or SandboxPolicy()
    # A pre-observed cancellation creates no worker process.
    if should_cancel is not None and should_cancel():
        return SandboxResult(
            returncode=_CANCELLED_RETURN_CODE,
            stdout="",
            stderr="cancelled",
            cancelled=True,
        )
    own_scratch = scratch_dir is None
    scratch = (
        Path(scratch_dir)
        if scratch_dir
        else Path(tempfile.mkdtemp(prefix="frisket-op-"))
    )
    controller: ProcessTreeController | None = None
    child_home: Path | None = None
    try:
        child_home = _child_home(scratch)
        env = _scrubbed_env(policy, extra_env or {}, default_home=child_home)
        child_argv = _fenced_argv(argv, policy)
        proc, controller, outer_cancelled = await _spawn_owned_process(
            child_argv,
            scratch=scratch,
            env=env,
            stdin=(
                asyncio.subprocess.PIPE if stdin_data else asyncio.subprocess.DEVNULL
            ),
            policy=policy,
            # NDJSON frames share the binary transport's existing byte budget;
            # asyncio's default 64 KiB line bound rejects valid table warnings.
            stream_limit=_SANDBOX_CHILD_FRAME_MAX_BYTES,
        )
        if outer_cancelled:
            await _teardown_cancelled_spawn(proc, controller)
            raise asyncio.CancelledError

        async def write_stdin() -> None:
            if stdin_data is None or proc.stdin is None:
                return
            try:
                proc.stdin.write(stdin_data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                return
            finally:
                proc.stdin.close()
                try:
                    await proc.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        async def read_stdout() -> None:
            if proc.stdout is None:
                return
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                on_stdout_line(line.decode(errors="replace").rstrip("\n"))

        async def read_stderr() -> bytes:
            if proc.stderr is None:
                return b""
            return await proc.stderr.read()

        stderr_task: asyncio.Future = asyncio.ensure_future(read_stderr())
        # One supervised task carries stdin/stdout/child-exit; the shared
        # teardown path settles it and the stderr reader together, so buffered
        # and streaming execution tear down identically.
        main_task: asyncio.Future = asyncio.ensure_future(
            asyncio.gather(write_stdin(), read_stdout(), proc.wait())
        )
        _, early = await _supervise_with_teardown(
            main_task,
            controller=controller,
            pipe_tasks=(main_task, stderr_task),
            should_cancel=should_cancel,
            wall_seconds=policy.wall_seconds,
        )
        if early is not None:
            return early
        stderr = await stderr_task
        result = SandboxResult(
            returncode=proc.returncode or 0,
            stdout="",
            stderr=stderr.decode(errors="replace"),
        )
        # Both one-shot doors classify, not just the one a caller happened to
        # use: a refusal that only the buffered entry point recognised is the
        # optional-wiring bug, one call site away from being missed.
        _raise_if_fence_refused(result, policy)
        return result
    finally:
        if controller is not None:
            controller.close()
        if child_home is not None:
            shutil.rmtree(child_home, ignore_errors=True)
        if own_scratch:
            shutil.rmtree(scratch, ignore_errors=True)


async def run_python_op(
    code: str,
    payload: dict,
    *,
    policy: SandboxPolicy | None = None,
    broker_endpoint: str | None = None,
) -> dict:
    """Run user python code in the sandbox. The code receives `payload` as
    JSON on stdin and must print a JSON result to stdout. Model access only
    via FRISKET_BROKER_ENDPOINT (see broker.py client helper)."""
    policy = policy or SandboxPolicy()
    if policy.allow_network:
        # There is no networked code recipe. Every caller today runs with the
        # default closed policy, and honouring `allow_network` here would mean
        # a second, unfenced shape of the same entry point -- the silent
        # degradation the fence exists to remove. A tool that must reach the
        # network is a repo-owned subprocess on `run_sandboxed`, not a recipe.
        raise ValueError(
            "run_python_op does not run code recipes with network access; "
            "use run_sandboxed for a repo-owned networked subprocess"
        )
    extra = {"FRISKET_PAYLOAD": "stdin"}
    if broker_endpoint:
        extra["FRISKET_BROKER_ENDPOINT"] = broker_endpoint
        # A broker is the ONLY thing that makes a socket creatable inside the
        # fence, and the one wall the kernel cannot hold up for us.
        fence.warn_broker_isolation_is_unenforced()
    guarded_code = code
    use_python_netwall = not _has_process_netwall(policy)
    if use_python_netwall:
        guarded_code = f"{PYTHON_NETWALL_BOOTSTRAP}\n{code}"
    # The kernel fence goes first: everything after it, including the audit
    # hook that backs it up in Python, runs already confined.
    guarded_code = (
        fence.recipe_prelude(allow_unix_sockets=bool(broker_endpoint)) + guarded_code
    )
    fence.warn_if_unenforced()
    result = await run_sandboxed(
        [sys.executable, "-I", "-c", guarded_code],
        policy=policy,
        stdin_data=json.dumps(payload).encode(),
        extra_env=extra,
    )
    if not result.ok:
        refusal = fence.refusal_reason(result.stderr)
        if refusal is not None:
            raise fence.SandboxEnforcementUnavailable(
                "refused to run a code recipe: this kernel cannot enforce the "
                f"sandbox fence ({refusal}). No recipe code ran. Frisket "
                "requires seccomp and Landlock on Linux; see "
                "engine/sandbox/fence.py for the platform matrix."
            )
        raise RuntimeError(
            f"sandboxed op failed (rc={result.returncode}, "
            f"timeout={result.timed_out}): {result.stderr[:500]}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"sandboxed op printed non-JSON: {result.stdout[:300]}"
        ) from e
