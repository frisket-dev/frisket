"""The kernel fence: what a sandboxed child is actually confined by.

This is the parent half. It serializes a narrow data-only policy for the fixed
runtime bootstrap, names the platforms, and turns a child that could not fence
itself into one named exception instead of a silent no-op run.

Two lanes use it, with one mechanism and two profiles:

  CODE RECIPES (`run_python_op`) get the generic profile baked into the child
  module -- run Python, import libraries, write scratch -- and REFUSE on a
  kernel that cannot enforce it.

  CONVERTERS (`SandboxPolicy.confine`, a `Confinement`) declare their own
  read/write/exec paths at the call site, because a converter's needs are
  specific and per run: this PDF, this output directory, this model cache.
  They DEGRADE LOUDLY on a kernel that cannot enforce, and the reason for the
  difference is written out at `warn_confinement_unavailable`.

Platform matrix -- what a code recipe is confined by, per platform:

  Linux    ENFORCED. seccomp-BPF refuses inet sockets, execve, the clones
           that would start a process (threads still work), ptrace,
           process_vm_*, io_uring and open_by_handle_at; Landlock confines
           the filesystem to the
           interpreter/stdlib/site-packages (read+execute) and the child's own
           scratch directory (read+write), and on ABI 4+ also refuses TCP
           bind/connect. Installed in the recipe child and PROVEN there before
           recipe code runs. If the kernel cannot do it, or cannot be shown to
           be doing it, the recipe REFUSES with
           `SandboxEnforcementUnavailable`. One wall is not kernel-enforced:
           see `BROKER_ISOLATION_IS_UNENFORCED`.
  macOS    PARTIAL. `sandbox-exec` (`shim._darwin_netwall_argv`) is a real
           OS-level network wall for the child and its subprocesses. There is
           NO filesystem confinement: the seatbelt profile is `(allow default)`
           plus network denials. Recipes run, and the gap is logged once per
           process.
  Windows  NONE below Python. No seccomp, no Landlock, no seatbelt; the only
           walls are the CPython audit hook and the scrubbed environment, both
           of which any `ctypes` call walks past. Recipes run, and this is
           logged once per process.

Why macOS and Windows warn instead of refusing. The promise is per platform,
and refusal is the answer where a promise cannot be kept. Linux promises
enforcement -- Linux is what Frisket deploys on, hosted and self-hosted -- so a
Linux kernel that cannot deliver it must refuse rather than pretend. macOS and
Windows never promised a kernel fence; they are development platforms here, and
turning `map.python` off on them would remove a working feature from every
developer in exchange for no user's safety. What they must not do is stay
quiet about it, which is what `warn_if_unenforced` is for. The UI says the same
thing at the code editor (`web/src/components/action-panel/ActionParams.tsx`).

Scope note: this fence covers `run_python_op` (the entry point behind
`map.python` recipes) and every caller that sets `SandboxPolicy.confine` --
today `to_markdown`'s markitdown worker, `media.extract_faces`,
`media.video_frames`' ffprobe/ffmpeg, OCR's pdftoppm rasterization and the
RapidOCR session worker. It does NOT cover the authoring workbench's plugin
subprocess (`frisket.plugins.subprocess_runner`, launched via `run_sandboxed`
with `allow_network=True`), which is repo-owned code on a declared-networked
policy, nor the transcription workers, which resolve model weights through a
Hugging Face cache that the sandbox's own HOME remap currently defeats --
confining them before that is fixed would turn a working (if wasteful) first
run into a hard failure.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import os
import sys
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# The child writes this to stderr and exits non-zero when it could not install
# or could not prove the fence. It is the wire between the two halves: the
# parent classifies on this marker, not on an exit code, because an exit code
# is one byte that any number of failures could produce.
FENCE_UNAVAILABLE_MARKER = "FRISKET_SANDBOX_FENCE_UNAVAILABLE:"


class SandboxEnforcementUnavailable(RuntimeError):
    """A code recipe refused to run because the kernel could not confine it.

    Raised in the parent when the recipe child reported that seccomp or
    Landlock could not be installed (or could not be proven) on this kernel.
    No recipe code ran: the fence goes up before the recipe body executes, so
    this is a refusal, never a partial execution.
    """


def bootstrap_policy(
    *,
    audit_netwall: bool,
    allow_unix_sockets: bool = False,
    confinement: Confinement | None = None,
    recipe: bool = False,
) -> dict[str, object]:
    """Return the only policy shape accepted by the managed bootstrap.

    ``recipe`` requests the built-in Python roots on Linux even without a
    caller-supplied converter profile. Other platforms preserve the historical
    audit-only/degraded posture by carrying no kernel-fence object.
    """

    profile: dict[str, object] | None = None
    if sys.platform == "linux" and (recipe or confinement is not None):
        profile = {
            "allow_unix_sockets": allow_unix_sockets,
            "read": list(confinement.read_paths() if confinement else ()),
            "write": list(confinement.write if confinement else ()),
            "allow_exec": bool(confinement and confinement.allow_exec),
            "python_roots": True if confinement is None else confinement.python_roots,
        }
    return {"version": 1, "audit_netwall": audit_netwall, "fence": profile}


# --- converter profiles ----------------------------------------------------
#
# The same two mechanisms, a different declared shape. A code recipe's needs
# are generic (run Python, import libraries, scribble in scratch) so its
# profile is baked into the child module. A converter's needs are specific and
# per run -- THIS pdf, THIS output directory, THIS model cache -- so they are
# declared by the op at the call site and travel in a `Confinement`.
#
# What makes that worth doing here rather than for recipes: the input is
# attacker-supplied (a PDF from a FOIA dump, a video from a source) and the
# code reading it is poppler/ffmpeg/OpenCV/onnxruntime, all with long CVE
# histories, while the job itself is exactly what Landlock expresses -- read
# one file, write one directory.


@dataclass(frozen=True)
class Confinement:
    """One op's declared filesystem needs inside the kernel fence.

    Every path is absolute and is granted as a Landlock rule: a directory
    grants its subtree, a plain file grants only itself, which is the tightest
    thing the op can say and the reason `read` usually names the single input
    file rather than the directory holding it.

    `exec_binary` switches on the native lane: the child is a Python launcher
    that installs the fence and then `execv`s into that program, so the filter
    must permit `execve` (see `_frisket_fence_build_filter`) and the
    interpreter roots are not granted. Leave it None and the child is a Python
    worker with `execve` refused outright, exactly as a recipe is.
    """

    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()
    exec_binary: str | None = None
    # What this op is called in the log when the kernel cannot confine it.
    op: str = "op"

    @property
    def allow_exec(self) -> bool:
        return self.exec_binary is not None

    @property
    def python_roots(self) -> bool:
        return self.exec_binary is None

    def read_paths(self) -> tuple[str, ...]:
        """`read`, plus the binary itself on the native lane.

        One mint site: a caller naming `exec_binary` never also has to
        remember to grant read+execute on it, which is the kind of optional
        second step that gets forgotten.
        """
        if self.exec_binary is None:
            return tuple(self.read)
        return (*self.read, self.exec_binary)


# --- can this kernel confine anything at all? ------------------------------

_LANDLOCK_CREATE_RULESET = 444
_SECCOMP_SET_MODE_FILTER = 1
_NR_SECCOMP = {"x86_64": 317, "aarch64": 277}


@functools.lru_cache(maxsize=1)
def kernel_can_confine() -> str | None:
    """None when this kernel can install the fence; else why it cannot.

    Both probes are read-only and safe to run in the SERVER process, which is
    the whole reason they are shaped this way: actually installing seccomp or
    Landlock here would permanently un-network the model router and un-file
    the store. `landlock_create_ruleset(NULL, 0, VERSION)` reports the ABI
    without creating anything; `seccomp(SET_MODE_FILTER, 0, NULL)` answers
    EFAULT when filtering is supported and ENOSYS/EINVAL when it is not,
    because it fails on the null pointer only after the support check.

    Cached: the answer is a property of the running kernel and cannot change
    under a live process.
    """
    if sys.platform != "linux":
        return f"{sys.platform} has no seccomp/Landlock"
    machine = os.uname().machine
    if machine not in _NR_SECCOMP:
        return f"no seccomp syscall table for CPU architecture {machine!r}"
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(
        _LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0), ctypes.c_uint32(1)
    )
    if abi < 1:
        return (
            "landlock is unavailable on this kernel (ABI probe returned "
            f"{abi}, errno {ctypes.get_errno()}); the filesystem cannot be confined"
        )
    ctypes.set_errno(0)
    libc.syscall(
        _NR_SECCOMP[machine],
        ctypes.c_uint(_SECCOMP_SET_MODE_FILTER),
        ctypes.c_uint(0),
        ctypes.c_void_p(None),
    )
    errno = ctypes.get_errno()
    if errno != 14:  # EFAULT: the syscall exists and filtering is supported
        return (
            "seccomp filtering is unavailable on this kernel "
            f"(SET_MODE_FILTER probe returned errno {errno})"
        )
    return None


_DEGRADED_OPS: set[str] = set()


def warn_confinement_unavailable(op: str, reason: str) -> None:
    """Say, once per op per process, that a converter is running unconfined.

    The decided posture for THIS lane, and the one place it differs from code
    recipes: degrade loudly instead of refusing. A recipe that refuses on an
    unfenceable kernel costs the operator one optional feature. A document
    import that refused would cost them the product -- importing a PDF is the
    core loop, the user did not choose to run code, and the state Frisket
    shipped in until now is exactly this unconfined one. Refusing would be a
    regression where degrading is only a missed improvement. What it must not
    do is be quiet about it.
    """
    if op in _DEGRADED_OPS:
        return
    _DEGRADED_OPS.add(op)
    logger.warning(
        "sandbox: %s runs UNCONFINED on this kernel (%s). It processes "
        "attacker-supplied files through poppler/ffmpeg/OpenCV/ONNX, and on a "
        "kernel with seccomp and Landlock it would be confined to its input "
        "and its output directory. See engine/sandbox/fence.py.",
        op,
        reason,
    )


def refusal_reason(stderr: str) -> str | None:
    """The child's fence complaint, if that is why it failed. Else None.

    Known and accepted: recipe code shares the child's stderr, so a recipe that
    DID run can print this marker itself and make its own failure look like a
    refusal. That is a misleading error message, not an escape -- a recipe can
    only do it from inside a fence that went up and stayed up, which is exactly
    what the message would be denying. Closing it properly means an extra
    inherited pipe closed before the recipe starts, which is plumbing through
    `run_sandboxed` that every other caller would pay for; not worth it until
    a real operator is misled by a real message.
    """
    for line in stderr.splitlines():
        if line.startswith(FENCE_UNAVAILABLE_MARKER):
            return line[len(FENCE_UNAVAILABLE_MARKER) :].strip()
    return None


_UNENFORCED_PLATFORM_NOTES = {
    "darwin": (
        "code recipes on macOS get the sandbox-exec network wall but NO "
        "filesystem confinement: recipe code can read and write any path this "
        "user can, including ~/.frisket/secrets/master.key. Linux hosts get "
        "seccomp + Landlock enforcement (engine/sandbox/fence.py)."
    ),
    "win32": (
        "code recipes on Windows have NO enforcement below Python: the CPython "
        "audit hook and the scrubbed environment are the only walls, and a "
        "ctypes call walks past both. Linux hosts get seccomp + Landlock "
        "enforcement (engine/sandbox/fence.py)."
    ),
}
_warned_platforms: set[str] = set()


def warn_if_unenforced() -> None:
    """Say once, per process, that this platform does not fence recipes."""
    note = _UNENFORCED_PLATFORM_NOTES.get(sys.platform)
    if note is None or sys.platform in _warned_platforms:
        return
    _warned_platforms.add(sys.platform)
    logger.warning("sandbox: %s", note)


BROKER_ISOLATION_IS_UNENFORCED = (
    "this code recipe was given a key-broker endpoint, so the fence must permit "
    "AF_UNIX sockets. Keeping them pointed at the broker is left to the CPython "
    "audit hook alone: seccomp cannot read connect()'s sockaddr pointer and "
    "Landlock ABI 4 does not govern unix-socket connects, so a ctypes call "
    "reaches any unix socket this user can. Pinned by "
    "tests/engine/test_sandbox_recipe_fence.py; the kernel-level answer is "
    "Landlock ABI 6 scoping or a socket the bootstrap connects before the fence "
    "closes."
)


def warn_broker_isolation_is_unenforced() -> None:
    """Say, every time, that a brokered recipe has one unenforced wall.

    Not once-per-process like the platform warning: a broker endpoint is passed
    per run, it is the one place where the fence's promises are weaker than
    they look, and today no production caller does it at all (pinned by a
    closure test). If that changes, the log should be noisy about it.
    """
    if sys.platform == "linux":
        logger.warning("sandbox: %s", BROKER_ISOLATION_IS_UNENFORCED)
