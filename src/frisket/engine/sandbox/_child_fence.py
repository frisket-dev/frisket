"""Linux kernel fence, installed INSIDE the child it confines.

The fixed runtime bootstrap loads this module by its exact absolute path before
domain code. It must stay stdlib-only, import nothing from `frisket`, and do
nothing at import time.

Two shapes call `_frisket_fence_install`. A `map.python` recipe calls it with
one argument and gets the generic profile below. A converter -- markitdown,
OpenCV, poppler, ffmpeg, the RapidOCR worker -- passes the paths it declared,
and on the native lane the child's remaining act is to `execv` into the
converter binary, which the fence permits for that lane alone. Everything
under "What goes up" is common to both.

Why in the child and not in the server: seccomp filters and Landlock rulesets
apply to the calling process and every descendant, and neither can be relaxed
afterwards. Installing either one in the Frisket server would permanently
un-network the model router and un-file the store the first time anybody ran a
recipe. The recipe already runs in its own forked+exec'd child
(`run_sandboxed` -> `asyncio.create_subprocess_exec`), so the fence goes up
there, in the window between interpreter start-up and the first line of recipe
code. Nothing attacker-controlled runs in that window: `-I` disables
PYTHONSTARTUP, PYTHONPATH and the user site directory, and recipe source stays
in the stdin request until the policy installer returns.

What goes up, in order:

1. `PR_SET_NO_NEW_PRIVS` -- required by both mechanisms, and on its own it
   stops any setuid binary from re-granting privileges.
2. A **seccomp-BPF** filter, refusing by argument where the argument is a
   scalar seccomp can actually read:
   - `socket()`/`socketpair()` are filtered on their DOMAIN. `AF_UNIX` is
     permitted only when the run has a key-broker endpoint to talk to; every
     other domain (`AF_INET`, `AF_INET6`, `AF_PACKET`, `AF_NETLINK`, ...)
     returns `EPERM`. No inet socket can be created, so no inet address can be
     connected to -- from Python, from `ctypes`, or from a loaded `.so`.
     `connect()` itself is NOT filtered: its address is a pointer, and seccomp
     cannot dereference pointers.
   - `clone()` is filtered on its FLAGS. `CLONE_THREAD` set means a thread,
     which recipes legitimately need (numpy and friends start them), and is
     allowed; without it the call would start a process, and is refused. That
     makes the Python-level `os.fork` refusal real below Python and takes the
     fork-bomb away. `clone3()` hides its flags behind a pointer, so it is
     refused with `ENOSYS` rather than `EPERM` -- glibc reads `ENOSYS` as "this
     kernel predates clone3" and retries through legacy `clone`, where the
     flags check applies.
   - flat refusals: `execve`/`execveat` (no new programs), `ptrace` and
     `process_vm_readv/writev` (same-uid memory reads of the server process),
     `kill`/`tkill`/`rt_sigqueueinfo`/`rt_tgsigqueueinfo`/
     `pidfd_send_signal` (signals to the guardian or another same-uid process;
     `tgkill` is limited to this process's own thread group),
     `io_uring_*` (its submission queue can issue file and socket operations
     that never appear as syscalls, which would bypass this filter),
     `open_by_handle_at` (resolves files without a path, which would bypass
     Landlock), and the namespace/module/keyring calls a recipe has no business
     making.
3. A **Landlock** ruleset (ABI 1+, best rights the running kernel offers).
   Read+execute on the interpreter, the stdlib, site-packages and the system
   library directories; read+write beneath the child's own scratch directory;
   nothing else. On ABI 4+ the ruleset additionally handles `bind`/`connect`
   over TCP and grants neither, so TCP is refused twice.

Then the installer PROVES the fence is up before returning: five refusals that
must come back `EPERM`, called through libc's wrappers so they submit the
numbers the running kernel uses rather than the numbers in the table below,
plus a directory that was never granted and must fail to open. A fence that
silently failed to install -- or a syscall number that is wrong for this
architecture -- becomes a refusal here instead of an open door.

Known limits, on purpose:

- Landlock ABI 4 does not govern metadata. `stat()`, `readlink()` and
  `access()` still work anywhere, so a recipe can learn that a path exists.
  It cannot read, write, list or execute it.
- Landlock ABI 4 does not govern `AF_UNIX` connect, and seccomp cannot read
  `connect`'s address. So when a run HAS a key broker -- the only case where
  AF_UNIX sockets exist at all -- "the recipe may only talk to the broker" is
  enforced by the CPython audit hook and nothing else, and a `ctypes` call
  walks past it to any unix socket this user can reach. No production caller
  passes a broker to a code recipe (a closure test in
  tests/engine/test_sandbox_recipe_fence.py keeps it that way), and without one
  no socket of any domain can be created. The kernel-level answers are Landlock
  ABI 6 (`LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET`, 6.12+) or a socket the bootstrap
  connects before it closes the fence.
- Everything importable is readable. `sys.path` is granted read+execute
  because that is what "can import" means, so on an editable install the
  project's own `src/` tree is readable by recipe code, and in any deployment
  site-packages is. Secrets are not there; `~/.frisket` is not granted.
- The filter is written for x86-64 and AArch64. Any other Linux architecture
  refuses rather than running unfenced.
"""

# --- syscall numbers -------------------------------------------------------
# Landlock's three syscalls are in the architecture-independent range, so they
# are the same everywhere. Everything else is per-architecture: x86-64 from
# arch/x86/entry/syscalls/syscall_64.tbl, AArch64 from the generic table in
# include/uapi/asm-generic/unistd.h.
_FRISKET_FENCE_LANDLOCK_CREATE_RULESET = 444
_FRISKET_FENCE_LANDLOCK_ADD_RULE = 445
_FRISKET_FENCE_LANDLOCK_RESTRICT_SELF = 446

# audit_arch values (linux/audit.h) -- the filter refuses any other personality.
_FRISKET_FENCE_AUDIT_ARCH_X86_64 = 0xC000003E
_FRISKET_FENCE_AUDIT_ARCH_AARCH64 = 0xC00000B7

_FRISKET_FENCE_ARCH_TABLES = {
    "x86_64": (
        _FRISKET_FENCE_AUDIT_ARCH_X86_64,
        317,  # __NR_seccomp
        {
            "socket": 41,
            "socketpair": 53,
            "clone": 56,
            "fork": 57,
            "vfork": 58,
            "clone3": 435,
            "execve": 59,
            "execveat": 322,
            "ptrace": 101,
            "process_vm_readv": 310,
            "process_vm_writev": 311,
            "kill": 62,
            "tkill": 200,
            "tgkill": 234,
            "rt_sigqueueinfo": 129,
            "rt_tgsigqueueinfo": 297,
            "pidfd_send_signal": 424,
            "unshare": 272,
            "setns": 308,
            "mount": 165,
            "umount2": 166,
            "pivot_root": 155,
            "chroot": 161,
            "bpf": 321,
            "perf_event_open": 298,
            "keyctl": 250,
            "add_key": 248,
            "request_key": 249,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "open_by_handle_at": 304,
            "init_module": 175,
            "finit_module": 313,
            "delete_module": 176,
            "kexec_load": 246,
        },
    ),
    "aarch64": (
        _FRISKET_FENCE_AUDIT_ARCH_AARCH64,
        277,  # __NR_seccomp
        {
            "socket": 198,
            "socketpair": 199,
            "clone": 220,
            # AArch64's generic table has no fork/vfork -- glibc implements
            # both through clone. None means "this architecture has no such
            # syscall to refuse", not "forgot to fill it in".
            "fork": None,
            "vfork": None,
            "clone3": 435,
            "execve": 221,
            "execveat": 281,
            "ptrace": 117,
            "process_vm_readv": 270,
            "process_vm_writev": 271,
            "kill": 129,
            "tkill": 130,
            "tgkill": 131,
            "rt_sigqueueinfo": 138,
            "rt_tgsigqueueinfo": 240,
            "pidfd_send_signal": 424,
            "unshare": 97,
            "setns": 268,
            "mount": 40,
            "umount2": 39,
            "pivot_root": 41,
            "chroot": 51,
            "bpf": 280,
            "perf_event_open": 241,
            "keyctl": 219,
            "add_key": 217,
            "request_key": 218,
            "io_uring_setup": 425,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "open_by_handle_at": 265,
            "init_module": 105,
            "finit_module": 273,
            "delete_module": 106,
            "kexec_load": 104,
        },
    ),
}

# --- Landlock access bits (linux/landlock.h) -------------------------------
_FRISKET_FENCE_FS_EXECUTE = 1 << 0
_FRISKET_FENCE_FS_WRITE_FILE = 1 << 1
_FRISKET_FENCE_FS_READ_FILE = 1 << 2
_FRISKET_FENCE_FS_READ_DIR = 1 << 3
_FRISKET_FENCE_FS_REMOVE_DIR = 1 << 4
_FRISKET_FENCE_FS_REMOVE_FILE = 1 << 5
_FRISKET_FENCE_FS_MAKE_CHAR = 1 << 6
_FRISKET_FENCE_FS_MAKE_DIR = 1 << 7
_FRISKET_FENCE_FS_MAKE_REG = 1 << 8
_FRISKET_FENCE_FS_MAKE_SOCK = 1 << 9
_FRISKET_FENCE_FS_MAKE_FIFO = 1 << 10
_FRISKET_FENCE_FS_MAKE_BLOCK = 1 << 11
_FRISKET_FENCE_FS_MAKE_SYM = 1 << 12
_FRISKET_FENCE_FS_REFER = 1 << 13  # ABI 2
_FRISKET_FENCE_FS_TRUNCATE = 1 << 14  # ABI 3
_FRISKET_FENCE_NET_BIND_TCP = 1 << 0  # ABI 4
_FRISKET_FENCE_NET_CONNECT_TCP = 1 << 1  # ABI 4

_FRISKET_FENCE_ABI1_FS = (
    _FRISKET_FENCE_FS_EXECUTE
    | _FRISKET_FENCE_FS_WRITE_FILE
    | _FRISKET_FENCE_FS_READ_FILE
    | _FRISKET_FENCE_FS_READ_DIR
    | _FRISKET_FENCE_FS_REMOVE_DIR
    | _FRISKET_FENCE_FS_REMOVE_FILE
    | _FRISKET_FENCE_FS_MAKE_CHAR
    | _FRISKET_FENCE_FS_MAKE_DIR
    | _FRISKET_FENCE_FS_MAKE_REG
    | _FRISKET_FENCE_FS_MAKE_SOCK
    | _FRISKET_FENCE_FS_MAKE_FIFO
    | _FRISKET_FENCE_FS_MAKE_BLOCK
    | _FRISKET_FENCE_FS_MAKE_SYM
)

_FRISKET_FENCE_READ_RIGHTS = (
    _FRISKET_FENCE_FS_EXECUTE | _FRISKET_FENCE_FS_READ_FILE | _FRISKET_FENCE_FS_READ_DIR
)
_FRISKET_FENCE_WRITE_RIGHTS = (
    _FRISKET_FENCE_READ_RIGHTS
    | _FRISKET_FENCE_FS_WRITE_FILE
    | _FRISKET_FENCE_FS_REMOVE_DIR
    | _FRISKET_FENCE_FS_REMOVE_FILE
    | _FRISKET_FENCE_FS_MAKE_CHAR
    | _FRISKET_FENCE_FS_MAKE_DIR
    | _FRISKET_FENCE_FS_MAKE_REG
    | _FRISKET_FENCE_FS_MAKE_SOCK
    | _FRISKET_FENCE_FS_MAKE_FIFO
    | _FRISKET_FENCE_FS_MAKE_BLOCK
    | _FRISKET_FENCE_FS_MAKE_SYM
    | _FRISKET_FENCE_FS_REFER
    | _FRISKET_FENCE_FS_TRUNCATE
)
# Rights that only mean something on a directory. Landlock rejects a rule with
# EINVAL when a non-directory is granted one of them, so a file rule is masked
# down to this set.
_FRISKET_FENCE_FILE_RIGHTS = (
    _FRISKET_FENCE_FS_EXECUTE
    | _FRISKET_FENCE_FS_READ_FILE
    | _FRISKET_FENCE_FS_WRITE_FILE
    | _FRISKET_FENCE_FS_TRUNCATE
)

# System roots a recipe needs in order to import anything: the shared-library
# search paths every extension module dlopen()s through, and the timezone and
# locale databases. Read-only distribution content, none of it Frisket's.
_FRISKET_FENCE_SYSTEM_READ_ROOTS = (
    "/usr/lib",
    "/usr/lib64",
    "/usr/local/lib",
    "/usr/share",
    "/lib",
    "/lib64",
)
# Individual files rather than the directories holding them. `/etc` in
# particular is NOT granted as a root: an operator can point
# FRISKET_SECRETS_KEY_DIR or FRISKET_DATA_DIR at a path under it
# (`team/security/secrets.py:_generated_key_path`), so granting the directory
# would hand the master key to a recipe on exactly the deployments most likely
# to care. These six are what glibc and CPython actually open -- the loader
# cache for every dlopen, the local timezone, and the nsswitch/passwd/group
# files behind `expanduser` and `getpass.getuser`.
_FRISKET_FENCE_READ_FILES = (
    "/etc/ld.so.cache",
    "/etc/localtime",
    "/etc/timezone",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/dev/urandom",
    "/dev/random",
    "/dev/zero",
    "/dev/full",
)
_FRISKET_FENCE_WRITE_FILES = ("/dev/null",)

# The Landlock proof. `/` is never granted (only subtrees of it are), and it
# exists on every Linux, so opening it for read must fail with EACCES once the
# ruleset is enforced. Anything else -- a success, or ENOENT from a path that
# does not exist -- means the fence is not doing what this module claims.
_FRISKET_FENCE_CANARY_DIR = "/"

# --- BPF ------------------------------------------------------------------
_FRISKET_FENCE_BPF_LD_W_ABS = 0x20
_FRISKET_FENCE_BPF_JEQ_K = 0x15
_FRISKET_FENCE_BPF_JGE_K = 0x35
_FRISKET_FENCE_BPF_RET_K = 0x06
_FRISKET_FENCE_SECCOMP_RET_ALLOW = 0x7FFF0000
_FRISKET_FENCE_SECCOMP_RET_KILL_PROCESS = 0x80000000
_FRISKET_FENCE_SECCOMP_SET_MODE_FILTER = 1
_FRISKET_FENCE_PR_SET_NO_NEW_PRIVS = 38
_FRISKET_FENCE_PR_GET_NO_NEW_PRIVS = 39
_FRISKET_FENCE_CLONE_THREAD = 0x00010000
_FRISKET_FENCE_BPF_AND_K = 0x54  # BPF_ALU | BPF_AND | BPF_K
_FRISKET_FENCE_ENOSYS = 38
_FRISKET_FENCE_AF_UNIX = 1
_FRISKET_FENCE_AF_INET = 2
_FRISKET_FENCE_EPERM = 1


class _FrisketFenceUnavailable(Exception):
    """The kernel fence could not be installed or could not be proven."""


def _frisket_fence_build_filter(
    audit_arch, table, allow_unix_sockets, allow_exec=False, self_pid=None
):
    """Assemble the classic-BPF program seccomp evaluates per syscall.

    Layout: check the personality, reject the x32 ABI, special-case the two
    socket-creating calls on their domain argument, then a linear chain of
    equality tests against the refused numbers, then allow.

    `allow_exec` drops `execve`/`execveat` from the deny chain. It exists for
    one shape only: the native-converter lane, where the fence is installed by
    a Python launcher that must then `execv` into ffmpeg/pdftoppm. The
    permission is narrower than it reads -- Landlock still governs WHICH files
    carry the EXECUTE right, so the launcher can exec the one binary its
    profile granted and the system library roots it dlopen()s through, and
    nothing else. Anything it does exec inherits this same filter and ruleset,
    because both are preserved across execve. A code recipe never sets it.
    """
    import struct

    def stmt(code, k):
        return struct.pack("=HBBI", code, 0, 0, k)

    def jump(code, k, jt, jf):
        return struct.pack("=HBBI", code, jt, jf, k)

    ld_abs = _FRISKET_FENCE_BPF_LD_W_ABS
    jeq = _FRISKET_FENCE_BPF_JEQ_K
    ret = _FRISKET_FENCE_BPF_RET_K
    kill = _FRISKET_FENCE_SECCOMP_RET_KILL_PROCESS
    ret_eperm = 0x00050000 | _FRISKET_FENCE_EPERM  # SECCOMP_RET_ERRNO(EPERM)
    # seccomp_data.nr is at offset 0, .arch at 4, .args[0] at 16 (little-endian
    # low word of the u64, which is where a socket domain lives).
    prog = [
        stmt(ld_abs, 4),
        jump(jeq, audit_arch, 1, 0),
        stmt(ret, kill),
        stmt(ld_abs, 0),
    ]
    if audit_arch == _FRISKET_FENCE_AUDIT_ARCH_X86_64:
        # x32 syscalls carry bit 30 and reuse x86-64 numbers; killing them
        # keeps the number table below meaningful.
        prog.append(jump(_FRISKET_FENCE_BPF_JGE_K, 0x40000000, 0, 1))
        prog.append(stmt(ret, kill))
    for name in ("socket", "socketpair"):
        if allow_unix_sockets:
            # nr == socket ? inspect the domain : skip this 5-instruction block
            prog.append(jump(jeq, table[name], 0, 4))
            prog.append(stmt(ld_abs, 16))
            prog.append(jump(jeq, _FRISKET_FENCE_AF_UNIX, 1, 0))
            prog.append(stmt(ret, ret_eperm))
            # AF_UNIX falls through to here; restore .nr for the chain below.
            prog.append(stmt(ld_abs, 0))
        else:
            prog.append(jump(jeq, table[name], 0, 1))
            prog.append(stmt(ret, ret_eperm))
    # `clone` makes both threads and processes, and a recipe legitimately needs
    # threads (numpy and friends start them). CLONE_THREAD is bit 16 of the
    # flags argument, which is arg0 and therefore a scalar seccomp can read --
    # so the two cases ARE separable: a clone that joins the current thread
    # group is allowed, a clone that starts a new process is refused. This is
    # what makes the Python-level `os.fork` audit refusal real below Python.
    prog.append(jump(jeq, table["clone"], 0, 5))
    prog.append(stmt(ld_abs, 16))
    prog.append(stmt(_FRISKET_FENCE_BPF_AND_K, _FRISKET_FENCE_CLONE_THREAD))
    prog.append(jump(jeq, 0, 0, 1))
    prog.append(stmt(ret, ret_eperm))
    prog.append(stmt(ld_abs, 0))
    # `clone3` takes a POINTER to its flags, which seccomp cannot dereference,
    # so it cannot be split the same way. Refuse it with ENOSYS rather than
    # EPERM: glibc's __clone_internal treats ENOSYS as "this kernel is too old
    # for clone3" and retries through legacy `clone`, where the check above
    # applies. EPERM would instead surface as a hard pthread_create failure.
    prog.append(jump(jeq, table["clone3"], 0, 1))
    prog.append(stmt(ret, 0x00050000 | _FRISKET_FENCE_ENOSYS))
    # A recipe otherwise has the same-uid authority to kill its guardian or
    # application host. Deny every process-directed signal API. Native thread
    # libraries legitimately use tgkill for pthread signals, so allow only a
    # tgkill whose tgid (arg0) is this process; the kernel itself then requires
    # the named tid to belong to that thread group.
    if self_pid is not None:
        prog.append(jump(jeq, table["tgkill"], 0, 4))
        prog.append(stmt(ld_abs, 16))
        prog.append(jump(jeq, self_pid, 1, 0))
        prog.append(stmt(ret, ret_eperm))
        prog.append(stmt(ld_abs, 0))
    for name in sorted(table):
        if name in ("socket", "socketpair", "clone", "clone3", "tgkill"):
            continue
        if allow_exec and name in ("execve", "execveat"):
            continue
        if table[name] is None:
            continue  # no such syscall on this architecture
        prog.append(jump(jeq, table[name], 0, 1))
        prog.append(stmt(ret, ret_eperm))
    prog.append(stmt(ret, _FRISKET_FENCE_SECCOMP_RET_ALLOW))
    return b"".join(prog)


def _frisket_fence_read_roots(extra=(), python_roots=True):
    """Directories the child may read and execute from.

    Derived, not guessed: a `map.python` recipe receives its row as JSON on
    stdin and returns JSON on stdout, so the only files it legitimately needs
    are the ones required to *run Python and import libraries*. That is the
    interpreter's own prefixes, everything already on `sys.path` (which is
    what "importable" means), and the system library directories any extension
    module dlopen()s through. The project directory, the user's home directory
    and ~/.frisket are deliberately absent, and so is `/etc` -- see
    `_FRISKET_FENCE_READ_FILES` for the six files granted individually instead.

    A converter profile adds `extra` (its input file, its model directory) on
    top. `python_roots=False` drops the interpreter half entirely: it is for
    the native lane, where the Python launcher's only remaining act is to
    `execv` into ffmpeg or pdftoppm, so site-packages and the project tree are
    not needed and are not granted.
    """
    import sys

    roots = []
    if python_roots:
        roots.extend([sys.base_prefix, sys.prefix])
        roots.extend(p for p in sys.path if p)
    roots.extend(_FRISKET_FENCE_SYSTEM_READ_ROOTS)
    return _frisket_fence_existing(roots, dirs_only=True) + _frisket_fence_existing(
        extra, dirs_only=False
    )


def _frisket_fence_existing(paths, dirs_only):
    """Real, existing, de-duplicated paths, in order.

    A profile's own entries may be single FILES (the one PDF this run
    converts), which is the tightest rule Landlock can express and the whole
    point of naming them; the interpreter roots are directories by
    construction and stay filtered to directories so this cannot quietly widen
    what a code recipe is granted.
    """
    import os

    seen = set()
    out = []
    for path in paths:
        try:
            real = os.path.realpath(path)
        except (OSError, ValueError):
            continue
        if real in seen:
            continue
        if not (os.path.isdir(real) if dirs_only else os.path.exists(real)):
            continue
        seen.add(real)
        out.append(real)
    return out


def _frisket_fence_write_roots(extra=()):
    """Directories the child may write to: its own scratch tree, plus profile.

    `run_sandboxed` spawns the child with `cwd` set to a per-run scratch
    directory and remaps HOME/TEMP/TMP to a directory beneath it, so one rule
    covers the working directory, the child home and every temporary file.
    A converter whose output the caller wants OUTSIDE that scratch tree (every
    one of them: the op owns the temp directory the row's outputs land in)
    names it in `extra`.
    """
    import os

    return [os.path.realpath(os.getcwd())] + _frisket_fence_existing(
        extra, dirs_only=False
    )


def _frisket_fence_install_landlock(read_roots, write_roots):
    import ctypes
    import os
    import stat
    import struct

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    abi = libc.syscall(
        _FRISKET_FENCE_LANDLOCK_CREATE_RULESET,
        None,
        ctypes.c_size_t(0),
        ctypes.c_uint32(1),
    )
    if abi < 1:
        raise _FrisketFenceUnavailable(
            "landlock is unavailable on this kernel "
            "(landlock_create_ruleset probe returned %d, errno %d); a code "
            "recipe cannot be confined to the filesystem without it"
            % (abi, ctypes.get_errno())
        )
    handled_fs = _FRISKET_FENCE_ABI1_FS
    if abi >= 2:
        handled_fs |= _FRISKET_FENCE_FS_REFER
    if abi >= 3:
        handled_fs |= _FRISKET_FENCE_FS_TRUNCATE
    handled_net = 0
    if abi >= 4:
        handled_net = _FRISKET_FENCE_NET_BIND_TCP | _FRISKET_FENCE_NET_CONNECT_TCP
        attr = struct.pack("=QQ", handled_fs, handled_net)
    else:
        attr = struct.pack("=Q", handled_fs)
    attr_buf = ctypes.create_string_buffer(attr, len(attr))
    ruleset = libc.syscall(
        _FRISKET_FENCE_LANDLOCK_CREATE_RULESET,
        attr_buf,
        ctypes.c_size_t(len(attr)),
        ctypes.c_uint32(0),
    )
    if ruleset < 0:
        raise _FrisketFenceUnavailable(
            "landlock_create_ruleset failed with errno %d" % ctypes.get_errno()
        )
    try:
        for paths, rights in (
            (read_roots, _FRISKET_FENCE_READ_RIGHTS & handled_fs),
            (_FRISKET_FENCE_READ_FILES, _FRISKET_FENCE_READ_RIGHTS & handled_fs),
            (write_roots, _FRISKET_FENCE_WRITE_RIGHTS & handled_fs),
            (_FRISKET_FENCE_WRITE_FILES, _FRISKET_FENCE_WRITE_RIGHTS & handled_fs),
        ):
            for path in paths:
                try:
                    fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
                except OSError:
                    # A root that does not exist on this distribution grants
                    # nothing; it cannot widen the fence, so skip it.
                    continue
                try:
                    allowed = rights
                    if not stat.S_ISDIR(os.stat(fd).st_mode):
                        allowed &= _FRISKET_FENCE_FILE_RIGHTS
                    rule = struct.pack("=Qi", allowed, fd)
                    rule_buf = ctypes.create_string_buffer(rule, len(rule))
                    rc = libc.syscall(
                        _FRISKET_FENCE_LANDLOCK_ADD_RULE,
                        ctypes.c_int(ruleset),
                        ctypes.c_uint32(1),  # LANDLOCK_RULE_PATH_BENEATH
                        rule_buf,
                        ctypes.c_uint32(0),
                    )
                    if rc < 0:
                        raise _FrisketFenceUnavailable(
                            "landlock_add_rule(%s) failed with errno %d"
                            % (path, ctypes.get_errno())
                        )
                finally:
                    os.close(fd)
        if libc.prctl(_FRISKET_FENCE_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
            raise _FrisketFenceUnavailable(
                "prctl(PR_SET_NO_NEW_PRIVS) failed with errno %d" % ctypes.get_errno()
            )
        rc = libc.syscall(
            _FRISKET_FENCE_LANDLOCK_RESTRICT_SELF,
            ctypes.c_int(ruleset),
            ctypes.c_uint32(0),
        )
        if rc < 0:
            raise _FrisketFenceUnavailable(
                "landlock_restrict_self failed with errno %d" % ctypes.get_errno()
            )
    finally:
        os.close(ruleset)


def _frisket_fence_install_seccomp(allow_unix_sockets, allow_exec=False):
    import ctypes
    import os

    machine = os.uname().machine
    entry = _FRISKET_FENCE_ARCH_TABLES.get(machine)
    if entry is None:
        raise _FrisketFenceUnavailable(
            "no seccomp syscall table for CPU architecture %r; Frisket refuses "
            "to run a code recipe it cannot confine (supported: %s)"
            % (machine, ", ".join(sorted(_FRISKET_FENCE_ARCH_TABLES)))
        )
    audit_arch, nr_seccomp, table = entry
    blob = _frisket_fence_build_filter(
        audit_arch, table, allow_unix_sockets, allow_exec, os.getpid()
    )
    count = len(blob) // 8

    class _SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    program = ctypes.create_string_buffer(blob, len(blob))
    fprog = _SockFprog(count, ctypes.cast(program, ctypes.c_void_p))
    if libc.prctl(_FRISKET_FENCE_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) < 0:
        raise _FrisketFenceUnavailable(
            "prctl(PR_SET_NO_NEW_PRIVS) failed with errno %d" % ctypes.get_errno()
        )
    rc = libc.syscall(
        nr_seccomp,
        ctypes.c_uint(_FRISKET_FENCE_SECCOMP_SET_MODE_FILTER),
        ctypes.c_uint(0),
        ctypes.byref(fprog),
    )
    if rc < 0:
        raise _FrisketFenceUnavailable(
            "seccomp(SECCOMP_SET_MODE_FILTER) failed with errno %d; this "
            "kernel has no seccomp filtering and a code recipe cannot be kept "
            "off the network without it" % ctypes.get_errno()
        )


# Syscalls the prover calls to check the filter answered, as (libc function,
# arguments, the table entry it certifies).
#
# They go through libc's WRAPPERS, never through `syscall(table[name])`. That
# distinction is the whole value: a probe that submits the same number the
# filter was built from is self-consistent and would pass with a wrong number
# in the table, which is precisely the AArch64 drift this is meant to catch.
# Calling `libc.unshare(0)` submits the number the running kernel uses, so
# EPERM means the fence and the kernel agree on what `unshare` is.
#
# Each entry is here because its UNFENCED result is distinguishable from EPERM
# -- measured: socket(AF_INET) -> a real fd, fork() -> a pid, unshare(0) -> 0,
# process_vm_readv with a zero pid -> 0, execve(NULL) -> EFAULT. A syscall that
# returns EPERM anyway when unfiltered (mount, keyctl, kexec_load,
# open_by_handle_at without CAP_DAC_READ_SEARCH) would prove nothing and is not
# probed. All five are harmless with these arguments; the fork probe is the one
# with a side effect if the fence is broken, and the prover exits that child
# immediately.
#
# What this certifies: the filter is loaded, the architecture/x32 preamble
# passes traffic, the socket() domain check and the clone() flags check both
# work, and four numbers spread across the linear deny chain are the ones the
# running kernel uses. What it does NOT certify: every remaining entry in the
# table. Those stay unverified on any architecture the developer is not sitting
# on, which is exactly why this list exists rather than a claim in a comment.
_FRISKET_FENCE_REFUSAL_PROBES = (
    ("socket", (_FRISKET_FENCE_AF_INET, 1, 0), "socket"),  # SOCK_STREAM
    ("fork", (), "clone"),  # glibc forks through clone; flags say "process"
    ("unshare", (0,), "unshare"),  # no flags: a no-op when permitted
    ("process_vm_readv", (0, None, 0, None, 0, 0), "process_vm_readv"),
    ("execve", (None, None, None), "execve"),  # NULL path: EFAULT, never execs
)
# Probes whose libc wrapper exists everywhere; if one of these is missing the
# fence cannot be verified at all and the recipe is refused rather than run.
_FRISKET_FENCE_REQUIRED_PROBES = ("socket", "fork")


def _frisket_fence_prove(allow_exec=False):
    """Refuse to hand control to recipe code unless the fence answers back.

    This is the difference between "we called the installer" and "the kernel is
    enforcing". A wrong syscall number, a filter the kernel quietly ignored, or
    a ruleset that never took effect turns into a refusal here instead of into
    a silently open fence.
    """
    import ctypes
    import errno as errno_mod
    import os

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_FRISKET_FENCE_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1:
        raise _FrisketFenceUnavailable("PR_SET_NO_NEW_PRIVS did not stick")
    _, _, table = _FRISKET_FENCE_ARCH_TABLES[os.uname().machine]
    probed = []
    for name, args, certifies in _FRISKET_FENCE_REFUSAL_PROBES:
        if allow_exec and certifies in ("execve", "execveat"):
            # This profile permits execve on purpose (the native lane execs
            # into its converter), so the probe has nothing to certify and its
            # unfenced answer -- EFAULT for a NULL path -- is not EPERM.
            continue
        call = getattr(libc, name, None)
        if call is None:
            if name in _FRISKET_FENCE_REQUIRED_PROBES:
                raise _FrisketFenceUnavailable(
                    "this libc has no %s wrapper, so the fence cannot be "
                    "verified; refusing rather than assuming" % name
                )
            continue
        ctypes.set_errno(0)
        rc = call(
            *[ctypes.c_void_p(None) if a is None else ctypes.c_long(a) for a in args]
        )
        err = ctypes.get_errno()
        if rc == 0 and name == "fork":
            # The filter is broken AND we are now the forked child. Leave
            # immediately: two processes must not go on to run the recipe.
            os._exit(126)
        if rc != -1 or err != _FRISKET_FENCE_EPERM:
            raise _FrisketFenceUnavailable(
                "seccomp filter installed but %s() returned rc=%d errno=%d "
                "instead of EPERM; the syscall filter is not in force, or the "
                "%r entry (%r) is wrong for this architecture"
                % (name, rc, err, certifies, table[certifies])
            )
        probed.append(name)
    for name in _FRISKET_FENCE_REQUIRED_PROBES:
        if name not in probed:
            raise _FrisketFenceUnavailable("fence probe %s did not run" % name)
    try:
        fd = os.open(_FRISKET_FENCE_CANARY_DIR, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        if exc.errno not in (errno_mod.EACCES, errno_mod.EPERM):
            raise _FrisketFenceUnavailable(
                "landlock canary %s failed with errno %d, expected EACCES; "
                "the ruleset is not in force" % (_FRISKET_FENCE_CANARY_DIR, exc.errno)
            ) from exc
    else:
        os.close(fd)
        raise _FrisketFenceUnavailable(
            "landlock ruleset installed but %s was still readable; the "
            "ruleset is not in force" % _FRISKET_FENCE_CANARY_DIR
        )


def _frisket_fence_install(
    allow_unix_sockets, read=(), write=(), allow_exec=False, python_roots=True
):
    """Install both mechanisms and prove them, or raise.

    Order between the two is immaterial -- no recipe code runs between them --
    so Landlock goes first simply because its install needs to open the roots
    it is about to grant, and reading the failure "landlock is unavailable"
    before "seccomp failed" is the more useful of the two orders on an old
    kernel, where Landlock is the newer requirement.

    Called with one argument this is the code-recipe profile, unchanged. The
    four defaulted parameters are what a converter profile varies: the extra
    paths it reads (its input, its model weights), the extra paths it writes
    (the op's own output directory), whether it may `execv` into a native
    converter, and whether it needs the interpreter roots at all.
    """
    _frisket_fence_install_landlock(
        _frisket_fence_read_roots(read, python_roots),
        _frisket_fence_write_roots(write),
    )
    _frisket_fence_install_seccomp(allow_unix_sockets, allow_exec)
    _frisket_fence_prove(allow_exec)
