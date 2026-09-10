from __future__ import annotations

import hashlib
import importlib
import inspect
from dataclasses import dataclass, field
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

MODULE = "frisket.plugins.managed_runtime"


# ---------------------------------------------------------------------------
# Defensive product-API accessor (frozen names the implementer must supply).
# ---------------------------------------------------------------------------


def _api() -> Any:
    """Resolve the not-yet-existing managed-runtime service module + its frozen
    exports, raising a semantic AssertionError when absent so a missing product
    seam is reported as an ordinary test failure."""
    assert find_spec(MODULE) is not None, (
        f"missing {MODULE}: the single managed-runtime service must "
        "fetch, verify, cache, and version a single-file "
        "executable runtime, expose its path, record the content hash in the "
        "receipt, and switch staged updates at the job boundary"
    )
    module = importlib.import_module(MODULE)
    required = [
        "ManagedRuntimeService",
        "ManagedRuntimeSpec",
        "RuntimeTier",
        "RuntimeNotInstalledError",
        "ChecksumMismatchError",
        "RuntimeQuarantinedError",
        "RuntimeSwitchNotAtJobBoundary",
        "HostedUpstreamFetchForbidden",
        "default_cache_root",
        "managed_runtime_receipt_evidence",
        "report_managed_runtime_states",
        "managed_runtime_cli",
    ]
    missing = [name for name in required if not hasattr(module, name)]
    assert not missing, f"{MODULE} lacks frozen exports: {missing}"
    return module


# ---------------------------------------------------------------------------
# Offline transport seam the service is expected to accept via `channel=`.
# The real channel does TLS + a SHA256SUMS-style manifest; the frozen check
# injects this fake so it runs with no network.
# ---------------------------------------------------------------------------


class _ChannelOffline(Exception):
    """Raised by the fake channel to model a release-fetch failure / offline."""


@dataclass
class _FakeChannel:
    # kind is the trust origin the service must gate on per tier:
    #   "upstream" — arbitrary official upstream (personal/self-host only)
    #   "mirror"   — first-party mirror (allowed on hosted)
    #   "bundled"  — image-bundled artifact (allowed on hosted)
    kind: str
    artifacts: dict[str, bytes] = field(default_factory=dict)
    checksums: dict[str, dict[str, str]] = field(default_factory=dict)
    offline: bool = False
    artifact_fetches: list[str] = field(default_factory=list)
    checksum_fetches: list[str] = field(default_factory=list)

    def fetch_artifact(self, url: str) -> bytes:
        self.artifact_fetches.append(url)
        if self.offline:
            raise _ChannelOffline(url)
        return self.artifacts[url]

    def fetch_checksums(self, url: str) -> dict[str, str]:
        self.checksum_fetches.append(url)
        if self.offline:
            raise _ChannelOffline(url)
        return self.checksums[url]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_ARTIFACT_URL = "https://example.invalid/yt-dlp/2025.01.01/yt-dlp"
_CHECKSUM_URL = "https://example.invalid/yt-dlp/2025.01.01/SHA256SUMS"
_ARTIFACT_BYTES = b"#!/bin/sh\necho yt-dlp 2025.01.01\n"


def _good_channel(kind: str = "upstream") -> _FakeChannel:
    return _FakeChannel(
        kind=kind,
        artifacts={_ARTIFACT_URL: _ARTIFACT_BYTES},
        checksums={_CHECKSUM_URL: {"yt-dlp": _sha256(_ARTIFACT_BYTES)}},
    )


def _spec(module: Any, cache_root: Path) -> Any:
    """A declared single-file managed runtime the service resolves. Kept
    decoupled from the manifest contract so this suite owns the SERVICE seam;
    plugin-runtime-gating-v1 owns the `managed_runtime` manifest field."""
    return module.ManagedRuntimeSpec(
        name="yt-dlp",
        version="2025.01.01",
        artifact_url=_ARTIFACT_URL,
        checksum_url=_CHECKSUM_URL,
        checksum_entry="yt-dlp",
    )


def _service(module: Any, cache_root: Path, *, tier: str = "personal") -> Any:
    tier_value = getattr(
        module.RuntimeTier, "PERSONAL" if tier == "personal" else "HOSTED"
    )
    return module.ManagedRuntimeService(cache_root=cache_root, tier=tier_value)


# ---------------------------------------------------------------------------
# 1. Content-addressed immutable cache: temp-write, verify, atomic rename.
# ---------------------------------------------------------------------------


def test_content_addressed_cache_is_immutable_and_verified_atomically(tmp_path):
    module = _api()
    cache_root = tmp_path / "runtimes"
    service = _service(module, cache_root)
    spec = _spec(module, cache_root)
    channel = _good_channel()

    resolution = service.resolve(spec, channel=channel)

    digest = _sha256(_ARTIFACT_BYTES)
    assert resolution.artifact_sha256 == digest, (
        "resolution must carry the content hash"
    )
    expected_dir = cache_root / "yt-dlp" / digest
    assert Path(resolution.cache_path) == expected_dir, (
        "artifacts must be content-addressed at <cache>/<name>/<sha256>/ "
        f"(got {resolution.cache_path})"
    )
    assert expected_dir.is_dir(), "the verified artifact dir must exist after resolve"
    exe = Path(resolution.executable_path)
    assert exe.is_file() and not exe.is_symlink(), (
        "resolved executable must be a real file under the content-addressed dir, "
        "not a symlink"
    )
    assert exe.read_bytes() == _ARTIFACT_BYTES

    # Atomic rename: no partial/temp staging dir left behind next to the final one.
    leftovers = [p.name for p in (cache_root / "yt-dlp").iterdir() if p.name != digest]
    assert not leftovers, (
        f"temp/partial staging must be cleaned after atomic rename: {leftovers}"
    )


# ---------------------------------------------------------------------------
# 2. Checksum mismatch fails closed: quarantined, never at the exec path.
# ---------------------------------------------------------------------------


def test_checksum_mismatch_is_quarantined_and_never_executed(tmp_path):
    module = _api()
    cache_root = tmp_path / "runtimes"
    service = _service(module, cache_root)
    spec = _spec(module, cache_root)

    # The channel serves bytes whose real digest does NOT match the manifest.
    tampered = b"#!/bin/sh\nrm -rf /\n"
    channel = _FakeChannel(
        kind="upstream",
        artifacts={_ARTIFACT_URL: tampered},
        checksums={_CHECKSUM_URL: {"yt-dlp": _sha256(_ARTIFACT_BYTES)}},
    )

    with pytest.raises(module.ChecksumMismatchError):
        service.resolve(spec, channel=channel)

    # The unverified bytes must NEVER land at any content-addressed exec path.
    claimed = _sha256(_ARTIFACT_BYTES)
    forged = _sha256(tampered)
    for digest in (claimed, forged):
        assert not (cache_root / "yt-dlp" / digest).exists(), (
            "a checksum-mismatched artifact must be quarantined, never placed at "
            "the content-addressed exec path"
        )


# ---------------------------------------------------------------------------
# 3. Exec-time digest re-check: a tampered cached artifact is refused.
# ---------------------------------------------------------------------------


def test_digest_is_rechecked_at_exec_time(tmp_path):
    module = _api()
    cache_root = tmp_path / "runtimes"
    service = _service(module, cache_root)
    spec = _spec(module, cache_root)

    resolution = service.resolve(spec, channel=_good_channel())
    exe = Path(resolution.executable_path)

    # Something corrupts the cached bytes after install.
    exe.write_bytes(b"#!/bin/sh\necho pwned\n")

    with pytest.raises((module.RuntimeQuarantinedError, module.ChecksumMismatchError)):
        # exec-time resolution must re-verify the digest, not trust the cache dir name.
        service.resolve_for_exec(spec)


def test_missing_current_runtime_is_distinct_from_quarantine(tmp_path):
    module = _api()
    service = _service(module, tmp_path / "runtimes")
    spec = _spec(module, tmp_path / "runtimes")

    with pytest.raises(module.RuntimeNotInstalledError):
        service.resolve_for_exec(spec)


# ---------------------------------------------------------------------------
# 4. Cache root is user-level, never inside a project workspace / bundle.
# ---------------------------------------------------------------------------


def test_cache_root_is_user_level_never_in_workspace(tmp_path, monkeypatch):
    module = _api()

    # Env-overridable; falls back to a user-level cache dir.
    root = module.default_cache_root()
    assert isinstance(root, (str, Path))
    root = Path(root)
    parts = set(root.parts)
    assert not ({".frisket"} & parts), (
        "managed-runtime cache must not live inside a project workspace/bundle "
        f"(got {root})"
    )
    # A project workspace / bundle path must be rejected as a cache root.
    workspace = tmp_path / "project" / ".frisket"
    workspace.mkdir(parents=True)
    with pytest.raises((ValueError, module.RuntimeQuarantinedError)):
        module.ManagedRuntimeService(
            cache_root=workspace,
            tier=module.RuntimeTier.PERSONAL,
        )


# ---------------------------------------------------------------------------
# 5. Tier-split resolution: hosted forbids arbitrary-upstream fetch.
# ---------------------------------------------------------------------------


def test_hosted_tier_resolves_only_from_first_party_never_upstream(tmp_path):
    module = _api()
    cache_root = tmp_path / "runtimes"
    spec = _spec(module, cache_root)

    hosted = _service(module, cache_root, tier="hosted")

    upstream = _good_channel(kind="upstream")
    with pytest.raises(module.HostedUpstreamFetchForbidden):
        hosted.resolve(spec, channel=upstream)
    assert upstream.artifact_fetches == [], (
        "hosted tier must not perform any arbitrary-upstream artifact fetch; "
        "hosted execution accepts only first-party mirrored artifacts"
    )

    # A first-party mirror IS allowed on hosted.
    mirror = _good_channel(kind="mirror")
    resolution = hosted.resolve(spec, channel=mirror)
    assert resolution.artifact_sha256 == _sha256(_ARTIFACT_BYTES)

    # Personal/self-host MAY use the official upstream channel.
    personal = _service(module, cache_root, tier="personal")
    ok = personal.resolve(spec, channel=_good_channel(kind="upstream"))
    assert ok.artifact_sha256 == _sha256(_ARTIFACT_BYTES)


# ---------------------------------------------------------------------------
# 6. Receipts record the content hash, not just a version string.
# ---------------------------------------------------------------------------


def test_receipt_evidence_records_artifact_content_hash(tmp_path):
    module = _api()
    cache_root = tmp_path / "runtimes"
    service = _service(module, cache_root)
    spec = _spec(module, cache_root)

    resolution = service.resolve(spec, channel=_good_channel())
    evidence = module.managed_runtime_receipt_evidence(resolution)

    assert set(evidence) >= {"runtime_name", "resolved_version", "artifact_sha256"}
    assert evidence["runtime_name"] == "yt-dlp"
    assert evidence["resolved_version"] == "2025.01.01"
    assert evidence["artifact_sha256"] == _sha256(_ARTIFACT_BYTES), (
        "the receipt must stamp the resolved CONTENT hash, not only a version "
        "string, because a version label alone does not identify the bytes run"
    )


# ---------------------------------------------------------------------------
# 7. Offline precedence: verified artifact runs offline; unverifiable staged
#    update is ignored; the current pointer is unchanged.
# ---------------------------------------------------------------------------


def test_offline_precedence_keeps_verified_artifact_and_ignores_staged_update(tmp_path):
    module = _api()
    cache_root = tmp_path / "runtimes"
    service = _service(module, cache_root)
    spec = _spec(module, cache_root)

    installed = service.resolve(spec, channel=_good_channel())
    pinned = installed.artifact_sha256

    # Offline exec: the previously verified artifact matching its digest runs
    # with no channel / an offline channel, raising no run error.
    offline_res = service.resolve_for_exec(spec)
    assert offline_res.artifact_sha256 == pinned

    # A staged update that cannot be verified (offline release fetch) is ignored:
    # it must not raise a run error and must not move the current pointer.
    offline_channel = _FakeChannel(kind="upstream", offline=True)
    service.stage_update(spec, channel=offline_channel)  # soft-degrade, no raise
    assert service.current_pointer("yt-dlp") == pinned, (
        "an unverifiable staged update must never switch the current runtime pointer"
    )


# ---------------------------------------------------------------------------
# 8. Job-boundary switching only, and the seam takes no queue internals.
# ---------------------------------------------------------------------------


def test_staged_update_applies_only_at_job_boundary(tmp_path):
    module = _api()
    cache_root = tmp_path / "runtimes"
    service = _service(module, cache_root)
    spec = _spec(module, cache_root)

    service.resolve(spec, channel=_good_channel())
    old = service.current_pointer("yt-dlp")

    # A newer verified version is staged.
    new_bytes = b"#!/bin/sh\necho yt-dlp 2025.02.02\n"
    new_artifact_url = "https://example.invalid/yt-dlp/2025.02.02/yt-dlp"
    new_checksum_url = "https://example.invalid/yt-dlp/2025.02.02/SHA256SUMS"
    new_spec = module.ManagedRuntimeSpec(
        name="yt-dlp",
        version="2025.02.02",
        artifact_url=new_artifact_url,
        checksum_url=new_checksum_url,
        checksum_entry="yt-dlp",
    )
    new_channel = _FakeChannel(
        kind="upstream",
        artifacts={new_artifact_url: new_bytes},
        checksums={new_checksum_url: {"yt-dlp": _sha256(new_bytes)}},
    )
    service.stage_update(new_spec, channel=new_channel)

    # Mid-job (not at a boundary) must be refused — never swap within a run id.
    with pytest.raises(module.RuntimeSwitchNotAtJobBoundary):
        service.apply_staged_update("yt-dlp", at_job_boundary=False)
    assert service.current_pointer("yt-dlp") == old

    # Between jobs the switch is allowed and moves the pointer.
    service.apply_staged_update("yt-dlp", at_job_boundary=True)
    assert service.current_pointer("yt-dlp") == _sha256(new_bytes)


def test_switch_seam_holds_no_queue_schema_or_claim_internals(tmp_path):
    module = _api()
    # The update/switch seam lives outside the queue implementation: its
    # parameters name a runtime + a boundary signal, never a queue,
    # claim, cursor, worker, or job-row handle. Tested at the seam, not by
    # importing queue internals.
    params = set(
        inspect.signature(module.ManagedRuntimeService.apply_staged_update).parameters
    )
    forbidden = {"queue", "claim", "cursor", "job_row", "worker", "connection", "conn"}
    assert not (params & forbidden), (
        "apply_staged_update must not thread queue schema/claim internals; "
        f"offending params: {sorted(params & forbidden)}"
    )


# ---------------------------------------------------------------------------
# 9. Doctor + CLI apply/inspect surface ship in this task.
# ---------------------------------------------------------------------------


def test_cli_apply_inspect_entrypoint_exists(tmp_path):
    module = _api()
    fn = module.managed_runtime_cli
    assert callable(fn), "a CLI apply/inspect entrypoint must exist"
    # An inspect invocation returns an exit code (0 = healthy report), never crashes.
    rc = fn(["inspect"])
    assert isinstance(rc, int)


# ---------------------------------------------------------------------------
# 10. A manifest with no managed-runtime block is unchanged.
# ---------------------------------------------------------------------------


def test_rung_one_manifest_without_managed_runtime_is_unaffected():
    """All managed-runtime fields are optional and absent by default; a
    manifest-only plugin validates with none of
    them present. (Green today AND after implementation — a durable guardrail.)"""
    from frisket.contracts.plugin import PluginManifest, PluginManifestContributes

    manifest = PluginManifest(
        id="acme.demo",
        version="1.0.0",
        contributes=PluginManifestContributes(actions=["acme.demo.thing"]),
    )
    assert getattr(manifest, "managed_runtime", None) is None, (
        "a manifest that declares no managed runtime must have none by default"
    )
