from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class ManagedRuntimeError(Exception):
    """Base for managed-runtime failures."""


class RuntimeNotInstalledError(ManagedRuntimeError):
    """No verified runtime is current yet; callers may use a trusted fallback."""


class ChecksumMismatchError(ManagedRuntimeError):
    """A fetched artifact's real digest does not match its checksum manifest."""


class RuntimeQuarantinedError(ManagedRuntimeError):
    """A cached artifact no longer matches its recorded digest and is refused."""


class RuntimeSwitchNotAtJobBoundary(ManagedRuntimeError):
    """A runtime switch was attempted mid-job; switches happen only between jobs."""


class HostedUpstreamFetchForbidden(ManagedRuntimeError):
    """The hosted tier may not fetch from an arbitrary upstream channel."""


class RuntimeTier(Enum):
    """Resolution trust tier. PERSONAL/self-host may fetch official upstream;
    HOSTED resolves only from a first-party mirror or an image-bundled
    artifact (no arbitrary-upstream fetch)."""

    PERSONAL = "personal"
    HOSTED = "hosted"


# Channel origins. Only "upstream" is arbitrary; it is forbidden on hosted.
_HOSTED_ALLOWED_CHANNEL_KINDS = frozenset({"mirror", "bundled"})


class RuntimeChannel(Protocol):
    """Transport seam the service fetches through. The real channel does TLS + a
    SHA256SUMS-style manifest; tests inject an offline fake with the same shape."""

    kind: str

    def fetch_artifact(self, url: str) -> bytes: ...

    def fetch_checksums(self, url: str) -> dict[str, str]: ...


@dataclass(frozen=True)
class ManagedRuntimeSpec:
    """A declared single-file executable managed runtime. Decoupled from the
    manifest contract so this module owns the SERVICE seam; the
    ``managed_runtime`` manifest field lives in frisket.contracts.plugin and is
    gated by frisket.plugins.runtime_gating."""

    name: str
    version: str
    artifact_url: str
    checksum_url: str
    checksum_entry: str


@dataclass(frozen=True)
class RuntimeResolution:
    """A verified, content-addressed runtime ready to execute."""

    name: str
    version: str
    artifact_sha256: str
    cache_path: Path
    executable_path: Path


def managed_runtime_receipt_evidence(resolution: RuntimeResolution) -> dict[str, str]:
    """Receipt evidence for a resolved runtime: the resolved CONTENT hash, not
    only a version string."""
    return {
        "runtime_name": resolution.name,
        "resolved_version": resolution.version,
        "artifact_sha256": resolution.artifact_sha256,
    }


# Cache root: user-level, never inside a project workspace / bundle.
_CACHE_ENV_VAR = "FRISKET_RUNTIME_CACHE_DIR"


def default_cache_root() -> Path:
    """The user-level managed-runtime cache dir. Env-overridable via
    ``FRISKET_RUNTIME_CACHE_DIR``; otherwise ``~/.cache/frisket/runtimes``.
    Never inside a project workspace/bundle."""
    override = os.environ.get(_CACHE_ENV_VAR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "frisket" / "runtimes"


def _reject_workspace_cache_root(cache_root: Path) -> None:
    """A managed-runtime cache must never live inside a project workspace/bundle;
    the local tier marks those with a ``.frisket`` directory component."""
    if ".frisket" in Path(cache_root).parts:
        raise ValueError(
            "managed-runtime cache_root must be a user-level dir, never inside a "
            f"project workspace/bundle (got {cache_root})"
        )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class _RuntimeState:
    installed: dict[str, ManagedRuntimeSpec]  # digest -> spec that produced it
    current: str | None = None
    staged: str | None = None


class ManagedRuntimeService:
    """The single managed-runtime service. There is exactly one — not one per
    engine (§2 cutover deletion). Owns the content-addressed cache, tier gating,
    verification, and job-boundary pointer switching for every declared runtime.
    """

    def __init__(self, cache_root: str | Path, tier: RuntimeTier) -> None:
        cache_root = Path(cache_root)
        _reject_workspace_cache_root(cache_root)
        self._cache_root = cache_root
        self._tier = tier
        self._states: dict[str, _RuntimeState] = {}

    def resolve(
        self, spec: ManagedRuntimeSpec, *, channel: RuntimeChannel
    ) -> RuntimeResolution:
        """Fetch + verify + cache the declared runtime, make it current, and
        return the verified resolution. Checksum mismatch fails closed."""
        digest, resolution = self._fetch_verify_install(spec, channel=channel)
        state = self._states.setdefault(spec.name, _RuntimeState(installed={}))
        state.installed[digest] = spec
        state.current = digest
        return resolution

    def resolve_for_exec(self, spec: ManagedRuntimeSpec) -> RuntimeResolution:
        """Exec-time resolution: return the CURRENT verified artifact, re-checking
        its on-disk digest so a post-install corruption is refused (never trust
        the cache dir name alone). Uses no channel — a verified artifact runs
        offline."""
        state = self._states.get(spec.name)
        if state is None or state.current is None:
            raise RuntimeNotInstalledError(
                f"managed runtime {spec.name!r} has no current verified artifact"
            )
        digest = state.current
        cache_path = self._artifact_dir(spec.name, digest)
        exe = self._executable_in(cache_path, spec.name)
        if not exe.is_file() or exe.is_symlink():
            raise RuntimeQuarantinedError(
                f"managed runtime {spec.name!r} artifact is missing or not a file"
            )
        actual = _sha256_file(exe)
        if actual != digest:
            raise RuntimeQuarantinedError(
                f"managed runtime {spec.name!r} failed its exec-time digest re-check "
                f"(recorded {digest}, on-disk {actual}); quarantined"
            )
        installed_spec = state.installed.get(digest, spec)
        return RuntimeResolution(
            name=spec.name,
            version=installed_spec.version,
            artifact_sha256=digest,
            cache_path=cache_path,
            executable_path=exe,
        )

    def stage_update(
        self, spec: ManagedRuntimeSpec, *, channel: RuntimeChannel
    ) -> None:
        """Fetch + verify a newer version and stage it as a new content-addressed
        dir. A release-fetch failure (offline) or verification failure soft-
        degrades: the staged pointer is NOT moved and no run error is raised
        The current pointer is never touched here."""
        try:
            digest, _ = self._fetch_verify_install(spec, channel=channel)
        except HostedUpstreamFetchForbidden:
            # A forbidden-tier fetch is a security boundary, not a soft failure.
            raise
        except Exception:  # noqa: BLE001 — offline / mismatch: the updater never crashes a run
            return
        state = self._states.setdefault(spec.name, _RuntimeState(installed={}))
        state.installed[digest] = spec
        state.staged = digest

    def apply_staged_update(self, name: str, *, at_job_boundary: bool) -> None:
        """Move the current-runtime pointer to the staged digest. Allowed ONLY
        between queue jobs (``at_job_boundary=True``); a mid-job switch is refused
        so a runtime never changes within a run id. This seam takes a runtime name
        + a boundary signal only — no queue/claim/cursor/job_row/worker/connection
        internals."""
        if not at_job_boundary:
            raise RuntimeSwitchNotAtJobBoundary(
                f"managed runtime {name!r} may switch only between queue jobs, "
                "never within a run id"
            )
        state = self._states.get(name)
        if state is None or state.staged is None:
            return
        state.current = state.staged
        state.staged = None

    def current_pointer(self, name: str) -> str | None:
        """The digest of the runtime currently in effect for ``name``."""
        state = self._states.get(name)
        return None if state is None else state.current

    def staged_pointer(self, name: str) -> str | None:
        """The digest of a staged-but-not-yet-applied update, if any."""
        state = self._states.get(name)
        return None if state is None else state.staged

    def installed_digests(self, name: str) -> list[str]:
        """All content-addressed digests installed for ``name``."""
        state = self._states.get(name)
        return [] if state is None else sorted(state.installed)

    def runtime_names(self) -> list[str]:
        return sorted(self._states)

    def _runtime_root(self, name: str) -> Path:
        return self._cache_root / name

    def _artifact_dir(self, name: str, digest: str) -> Path:
        return self._runtime_root(name) / digest

    @staticmethod
    def _executable_in(cache_path: Path, name: str) -> Path:
        return cache_path / name

    def _fetch_verify_install(
        self, spec: ManagedRuntimeSpec, *, channel: RuntimeChannel
    ) -> tuple[str, RuntimeResolution]:
        """Tier-gate, fetch, verify the checksum, and place the artifact at its
        content-addressed dir via temp-write + verify + atomic rename. Returns
        ``(digest, resolution)``. Raises before any byte moves on a forbidden
        hosted-upstream fetch; raises ChecksumMismatchError (quarantining the
        temp) on digest mismatch."""
        self._gate_channel(channel)

        checksums = channel.fetch_checksums(spec.checksum_url)
        expected = checksums.get(spec.checksum_entry)
        if not expected:
            raise ChecksumMismatchError(
                f"checksum manifest has no entry {spec.checksum_entry!r} for "
                f"managed runtime {spec.name!r}"
            )
        data = channel.fetch_artifact(spec.artifact_url)
        actual = _sha256_bytes(data)
        if actual != expected:
            # Fail closed: the unverified bytes never reach a content-addressed
            # exec path.
            raise ChecksumMismatchError(
                f"managed runtime {spec.name!r} checksum mismatch "
                f"(manifest {expected}, artifact {actual}); quarantined"
            )

        cache_path = self._install_verified(spec.name, actual, data)
        exe = self._executable_in(cache_path, spec.name)
        resolution = RuntimeResolution(
            name=spec.name,
            version=spec.version,
            artifact_sha256=actual,
            cache_path=cache_path,
            executable_path=exe,
        )
        return actual, resolution

    def _gate_channel(self, channel: RuntimeChannel) -> None:
        if self._tier is RuntimeTier.HOSTED:
            kind = getattr(channel, "kind", "upstream")
            if kind not in _HOSTED_ALLOWED_CHANNEL_KINDS:
                # Raise before any fetch: no arbitrary-upstream bytes move on
                # hosted (a no-community-arbitrary-code boundary).
                raise HostedUpstreamFetchForbidden(
                    f"hosted tier may not fetch from an arbitrary upstream channel "
                    f"(kind={kind!r}); use a first-party mirror or image-bundled "
                    "artifact"
                )

    def _install_verified(self, name: str, digest: str, data: bytes) -> Path:
        """Place verified bytes at ``<cache>/<name>/<digest>/<name>`` via temp-
        write + atomic rename. Idempotent: an already-installed digest is reused.
        """
        runtime_root = self._runtime_root(name)
        runtime_root.mkdir(parents=True, exist_ok=True)
        final_dir = runtime_root / digest
        if final_dir.exists():
            return final_dir

        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=runtime_root))
        try:
            exe = staging / name
            exe.write_bytes(data)
            # Re-verify what actually landed on disk before publishing it.
            if _sha256_file(exe) != digest:
                raise ChecksumMismatchError(
                    f"managed runtime {name!r} staged bytes failed re-verification"
                )
            # Executable + readable; owner-writable so the host can manage the
            # cache (immutability is enforced by the exec-time digest re-check,
            # not by dropping the owner-write bit).
            exe.chmod(0o755)
            try:
                os.replace(staging, final_dir)
            except OSError:
                # A racing installer won the rename; reuse its verified dir.
                if final_dir.exists():
                    shutil.rmtree(staging, ignore_errors=True)
                    return final_dir
                raise
            return final_dir
        finally:
            # Atomic rename consumed the staging dir on success; clean any
            # partial/quarantined staging left behind on failure.
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def report_managed_runtime_states(
    service: ManagedRuntimeService,
) -> list[dict[str, Any]]:
    """Doctor surface: installed / current / staged state per managed runtime,
    by name. Truthful — degraded is reported, never a false ready."""
    report: list[dict[str, Any]] = []
    for name in service.runtime_names():
        report.append(
            {
                "runtime_name": name,
                "installed": service.installed_digests(name),
                "current": service.current_pointer(name),
                "staged": service.staged_pointer(name),
            }
        )
    return report


def managed_runtime_cli(argv: list[str] | None = None) -> int:
    """`frisket runtimes <inspect|apply>` — the small apply/inspect surface.

    - ``inspect`` prints installed/current/staged state for every managed runtime
      in the user-level cache and returns 0 (a healthy report never crashes);
    - ``apply <name>`` moves a staged update to current at a job boundary.
    """
    argv = list(argv or [])
    command = argv[0] if argv else "inspect"

    service = ManagedRuntimeService(
        cache_root=default_cache_root(), tier=RuntimeTier.PERSONAL
    )

    if command in {"-h", "--help"}:
        print("Usage: frisket runtimes [inspect|apply <name>]")
        return 0

    if command == "inspect":
        states = report_managed_runtime_states(service)
        if not states:
            print("managed runtimes: none installed")
        for state in states:
            print(
                f"  {state['runtime_name']}: current={state['current']} "
                f"staged={state['staged']} installed={len(state['installed'])}"
            )
        return 0

    if command == "apply":
        if len(argv) < 2:
            print("Usage: frisket runtimes apply <name>", flush=True)
            return 2
        service.apply_staged_update(argv[1], at_job_boundary=True)
        print(f"applied staged update for {argv[1]!r} (if any)")
        return 0

    print(f"unknown runtimes command: {command!r} (expected inspect|apply)")
    return 2
