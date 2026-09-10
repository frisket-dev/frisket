"""Shared self-probe logic and remediation guidance.

`frisket doctor` (cli.py) was the only caller of these probes; this module
extracts them so an in-app Diagnose panel (server/routes/diagnose.py) can
compute the SAME facts as JSON, over HTTP, without duplicating the logic. Each
probe returns a small structured dict rather than a pre-formatted string, so
both the CLI's plain-text printer and the JSON route can render it their own
way from one source of truth.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import Any

from frisket.redaction import redact_text
from frisket.engine._workers.rapidocr_models import (
    rapidocr_default_model_requirements,
    rapidocr_model_root_complete,
)


def python_version_report() -> dict[str, Any]:
    return {"ok": True, "version": sys.version.split()[0]}


def store_roundtrip_report() -> dict[str, Any]:
    """CORE probe: the project store actually round-trips data and the
    search sidecar indexes it. Raises on failure — callers decide whether
    that flips a "healthy" bit (cli.py) or just an "ok": False (the API
    route never lets a probe failure 500 the whole diagnose response)."""
    from frisket.search import rebuild_index, search_project
    from frisket.engine.store import Project

    with tempfile.TemporaryDirectory() as td:
        p = Project.create(Path(td) / "doctor.frisket", name="doctor")
        try:
            sheet = p.add_sheet("probe")
            cols = {"note": p.add_column(sheet, "note")}
            p.add_rows(sheet, [{"note": "doctor probe row"}], cols)
            (val,) = p.get_values(sheet, cols["note"]).values()
            assert val == "doctor probe row", f"read-back mismatch: {val!r}"
            n = rebuild_index(p)
            hits = search_project(p, "doctor")
            assert n >= 1 and hits, "search sidecar found nothing"
        finally:
            p.close()
    return {"ok": True, "detail": "create → write → read → fts all OK"}


def provider_report(router: Any | None = None) -> dict[str, Any]:
    """Provider key presence only — never values (keys
    are a key-broker concern, doctor/diagnose never sees or reports them)."""
    from frisket.ai.llm import ModelRouter

    router = router or ModelRouter()
    provs = router.providers()
    keyed = [x for x in provs if x != "ollama"]
    local_configured = bool(router.local_endpoints)
    configured = [*keyed, *(["local endpoints"] if local_configured else [])]
    return {
        "configured": keyed,
        "keyless_ok": True,
        "summary": (
            ", ".join(configured)
            if configured
            else "no API keys configured (local tier works keyless via cache/replay)"
        ),
    }


def local_model_endpoints_report(
    *, timeout: float = 2.0, router: Any | None = None
) -> dict[str, Any]:
    """Probe every explicitly configured local-model endpoint.

    The report is plural and empty when no endpoint authority exists. It never
    invents localhost and never chooses a first/primary server.
    """
    if router is None:
        from frisket.ai.llm.endpoint_config import resolve_env_local_endpoint

        endpoint, _notes = resolve_env_local_endpoint()
        endpoints = (endpoint,) if endpoint is not None else ()
    else:
        endpoints = tuple(router.local_endpoints)

    from frisket.server.provider_config import ollama_reachable

    reports: list[dict[str, Any]] = []
    for endpoint in endpoints:
        try:
            probe = ollama_reachable(
                endpoint.origin,
                timeout=timeout,
                token=endpoint.inference_token,
                edge_auth=endpoint.edge_auth,
            )
            reports.append(
                {
                    "endpoint_id": endpoint.endpoint_id,
                    "display_name": endpoint.display_name,
                    "origin": endpoint.origin,
                    "source": endpoint.source,
                    "reachable": bool(probe.get("reachable")),
                    "models": list(probe.get("models") or []),
                    "protocol": probe.get("protocol", "unknown"),
                    "auth_status": probe.get("auth_status", "unknown"),
                    "error": probe.get("detail"),
                }
            )
        except Exception as error:  # noqa: BLE001 — report, never raises
            reports.append(
                {
                    "endpoint_id": endpoint.endpoint_id,
                    "display_name": endpoint.display_name,
                    "origin": endpoint.origin,
                    "source": endpoint.source,
                    "reachable": False,
                    "models": [],
                    "error": redact_text(
                        str(error),
                        secret_values=(
                            endpoint.inference_token,
                            endpoint.provisioning_token,
                        ),
                        max_chars=300,
                    ),
                }
            )
    return {
        "configured": len(reports),
        "reachable": sum(1 for report in reports if report["reachable"]),
        "endpoints": reports,
    }


def provider_key_validation_report(router: Any | None = None) -> dict[str, Any]:
    """Per-provider key VALIDITY (decision 9), one step past
    ``provider_report``'s presence-only check.

    Cross-model review fix: the first cut of this probe delegated to
    ``ModelRouter.probe_providers`` (router.py), which sends a bare
    unauthenticated ``GET`` -- no ``Authorization``/``x-api-key`` header at
    all -- so it reported a genuinely VALID key as "rejected" (it never sent
    the key to reject). This now reuses the SAME authenticated probe
    Settings -> AI Providers already validates a freshly-entered key with
    (``frisket.server.provider_config.probe_provider``): a real per-provider
    auth header, one sync ``httpx.Client`` shared across providers and
    always closed -- no async client-lifecycle entanglement with the
    router's own pooled client. That function's
    own contract is to never return the key value; this probe adds nothing
    that could leak it either.

    Status interpretation: 200 = valid; 401/403 = the key was rejected
    (INVALID, matches ``frisket.llm.remediation.classify_resumable_
    provider_error``'s ``invalid_provider_key`` class); any other >=400
    (429 rate-limited, 5xx) or a network failure is INDETERMINATE -- the
    host answered something-other-than-200 or didn't answer, but that is
    not evidence the key itself is bad, and a naive ">=400 is invalid, else
    accepted" reading (the pre-fix bug) let a 429/500 summarize as
    "accepted". Local endpoints are excluded because their endpoint-scoped
    credentials and reachability are covered by
    ``local_model_endpoints_report`` above."""
    from frisket.ai.llm import ModelRouter
    from frisket.server.provider_config import probe_provider

    router = router or ModelRouter()
    keys = {
        name: key for name, key in router.configured_keys().items() if name != "ollama"
    }
    if not keys:
        return {
            "providers": {},
            "summary": "no keyed providers configured (replay only)",
        }

    import httpx

    results: dict[str, dict[str, Any]] = {}
    invalid: list[str] = []
    indeterminate: list[str] = []
    valid_names: list[str] = []
    try:
        with httpx.Client() as client:
            for name, key in keys.items():
                probed = probe_provider(name, key, client=client, timeout=3.0)
                status = probed.get("status")
                if not probed.get("reachable"):
                    results[name] = {"valid": None, "reachable": False, "status": None}
                    indeterminate.append(name)
                elif status == 200:
                    results[name] = {"valid": True, "reachable": True, "status": status}
                    valid_names.append(name)
                elif status in (401, 403):
                    results[name] = {
                        "valid": False,
                        "reachable": True,
                        "status": status,
                    }
                    invalid.append(name)
                else:
                    # 429/5xx/etc: the host answered, but a rate limit or a
                    # transient provider failure says nothing about whether
                    # the key itself is good.
                    results[name] = {"valid": None, "reachable": True, "status": status}
                    indeterminate.append(name)
    except Exception as e:  # noqa: BLE001 — validation probe, never raises
        return {
            "providers": {},
            "summary": f"provider key validation unavailable ({e})",
        }

    if invalid:
        summary = f"key rejected: {', '.join(invalid)}"
    elif indeterminate and valid_names:
        summary = f"{', '.join(valid_names)} accepted; indeterminate (rate-limited/unreachable): {', '.join(indeterminate)}"
    elif indeterminate:
        summary = (
            f"indeterminate (rate-limited/unreachable): {', '.join(indeterminate)}"
        )
    else:
        summary = f"{', '.join(valid_names)} key(s) accepted"
    return {"providers": results, "summary": summary}


def replay_mode_report(router: Any | None = None) -> dict[str, Any]:
    """Whether the effective router can make a LIVE model call right now
    (decision 9).

    This is the ADAPTER-AWARE variant of ``frisket.llm.router.
    live_calls_possible``: the helper reads mode only (``replay_strict`` ->
    no live calls, everything else -> live calls possible), which matches the
    ACTUAL router behaviour -- ``frisket.llm.cache``'s module docstring is
    explicit ("replay -- hit returns cached; miss does live call then stores")
    and ``ModelRouter._complete_transport`` (router.py) really does fall
    through to ``_call_with_retry`` on a replay-mode miss. This report adds the
    second axis the mode-only helper can't see: whether a ``ResponseCache`` is
    actually attached. ``cache is None`` skips the mode branch entirely and
    goes straight to a live call, so even ``replay_strict`` makes live calls
    with no cache; only ``replay_strict`` WITH a cache genuinely blocks a miss
    (raises ``CacheMiss`` instead of calling out). Endpoint configuration and
    cache posture are independent facts; this probe reports only whether the
    effective cache mode blocks live calls."""
    from frisket.ai.llm import ModelRouter

    router = router or ModelRouter()
    mode = getattr(router, "cache_mode", "replay")
    cache_configured = getattr(router, "cache", None) is not None
    blocks_live_calls = mode == "replay_strict" and cache_configured
    if blocks_live_calls:
        summary = (
            "replay_strict (cached only -- a cache miss raises instead of "
            "calling a live provider)"
        )
    elif mode == "replay" and cache_configured:
        summary = (
            "replay (cache hits replay; a cache MISS falls through to a "
            "live provider call if one is configured)"
        )
    elif mode in ("replay", "replay_strict") and not cache_configured:
        summary = f"{mode} requested but no cache is attached -- every call is live"
    else:
        summary = f"{mode} (every call is live)"
    return {
        "cache_mode": mode,
        "cache_configured": cache_configured,
        "live_calls_possible": not blocks_live_calls,
        "summary": summary,
    }


def queue_health_report(
    queue: Any | None = None,
    *,
    liveness_window_seconds: float = 90.0,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Worker heartbeat / queue-staleness (decision 9: "built in the
    queue-health wave; needs exposure here"). Wraps ``frisket.jobs.
    queue_health.queue_health_payload`` -- the SAME DTO ``/api/health``
    already serves -- so Diagnostics and the health endpoint can never
    disagree about whether a queued job is stuck behind a dead worker.
    ``queue=None`` (bare ``frisket doctor``, which opens no queue) is a
    quiet skip, same idiom as ``models_sidecar_report``'s absent-env skip."""
    if queue is None:
        return {
            "configured": False,
            "summary": "no run queue in scope for this probe",
        }
    from datetime import UTC, datetime

    from frisket.engine.jobs.queue_health import queue_health_payload

    payload = queue_health_payload(
        queue,
        now=datetime.now(UTC),
        liveness_window_seconds=liveness_window_seconds,
        timeout_seconds=timeout_seconds,
    )
    workers = payload["workers"]
    queued = payload["queued"]
    if queued["no_live_worker"]:
        summary = (
            f"{queued['count']} job(s) queued, NO live worker "
            f"(oldest waiting {round(queued['oldest_age_seconds'] or 0)}s)"
        )
    else:
        summary = f"{workers['live']} live worker(s), {queued['count']} job(s) queued"
    return {"configured": True, "summary": summary, **payload}


def disk_free_report(path: str | Path | None = None) -> dict[str, Any]:
    """Free disk under the workspace (decision 9): a project bundle or job
    queue silently failing writes on a full disk is one of the least
    self-diagnosing failure modes there is. ``path`` defaults to the current
    working directory when no workspace root is known (bare ``frisket
    doctor``); the server route always passes the real workspace root."""
    target = Path(path) if path is not None else Path.cwd()
    probe_path = target if target.exists() else target.parent
    try:
        usage = shutil.disk_usage(probe_path)
    except OSError as e:  # noqa: BLE001 — report, never raise
        return {"path": str(target), "summary": f"disk usage unavailable ({e})"}
    free_gb = round(usage.free / (1024**3), 1)
    total_gb = round(usage.total / (1024**3), 1)
    low = usage.free < 2 * (1024**3)
    return {
        "path": str(target),
        "free_gb": free_gb,
        "total_gb": total_gb,
        "low": low,
        "summary": (
            f"{free_gb} GB free of {total_gb} GB at {target}"
            + (" (LOW)" if low else "")
        ),
    }


def plugin_health_report(
    project: Any | None = None, *, project_id: str | None = None
) -> dict[str, Any]:
    """Plugin runtime health (decision 10: "N installed, M active, K failed
    (names)") -- the probe the plugins-tab removal routes its health surface
    into instead of the read-only dock duplicate. Projects installed/enabled/
    failed state (``workbench.plugin_runtime.workbench_plugin_runtime_index``,
    the SAME projection ``PluginManager`` reads) into three counts + the
    failed plugin ids, so a broken plugin install is visible from Diagnostics
    without opening Settings → Plugins. ``project=None`` (no project in
    scope -- ``/api/diagnose`` has none by default, same as
    ``diagnostic_router``'s docstring) is an honest skip, not a failure.

    Cross-model review fixes (Lane C depends on this shape being both
    truthful and stable):

    - ``installed`` now excludes ``installState == "uninstalled"`` entries.
      The runtime index keeps a ledger row for a plugin the operator has
      removed (``_runtime_plugin_from_manifest_ref``,
      ``workbench/plugin_runtime.py``); counting that row as still-installed
      previously left the count unchanged after an uninstall.
    - ``active`` now reads the runtime registry signal
      (``registryActivated``), not the persisted ``installState``. A fresh
      process restart has no in-memory registrations yet even for a plugin
      persisted as ``"enabled"`` -- the persisted field previously reported
      plugins as active when their real in-memory registration was false.
    - Goes through the SAME bootstrap seam
      ``server/services/workbench.py``'s ``WorkbenchService.plugin_index``
      uses (``bootstrap_project_bundled_plugins``) before reading the index,
      so a project created before this probe ran sees the same counts the
      canonical Settings -> Plugins surface would. Idempotent by contract
      (``bootstrap_project_bundled_plugins``'s own docstring); this module
      has no per-request instance to cache an "already bootstrapped" flag on
      the way ``WorkbenchService`` does, so it runs every call -- safe, not
      free.
    - The stable 6-field shape (``available``, ``installed``, ``active``,
      ``failed``, ``failed_names``, ``summary``) is now guaranteed even when
      the index read raises, instead of collapsing to ``_info_probe``'s
      generic ``{ok, error, summary}`` on failure -- Lane C's caller should
      never have to branch on two different shapes for the same probe.
    """
    if project is None or project_id is None:
        return {
            "available": False,
            "installed": 0,
            "active": 0,
            "failed": 0,
            "failed_names": [],
            "summary": "no project in scope (open a project to see plugin health)",
        }
    try:
        from frisket.authoring.workbench.plugin_runtime import (
            bootstrap_project_bundled_plugins,
        )
        from frisket.authoring.workbench.plugin_runtime_status import (
            workbench_plugin_runtime_index,
        )

        bootstrap_project_bundled_plugins(project, project_id=project_id)
        index = workbench_plugin_runtime_index(project, project_id=project_id)
    except Exception as e:  # noqa: BLE001 — keep the stable 6-field shape on failure
        return {
            "available": False,
            "installed": 0,
            "active": 0,
            "failed": 0,
            "failed_names": [],
            "summary": f"plugin health unavailable ({e})",
        }
    plugins = index.get("plugins") or []
    live = [p for p in plugins if p.get("installState") != "uninstalled"]
    installed = len(live)
    active = sum(1 for p in live if p.get("registryActivated") is True)
    failed_names = [
        str(p.get("pluginId")) for p in live if p.get("installState") == "failed"
    ]
    failed = len(failed_names)
    summary = f"{installed} installed, {active} active, {failed} failed"
    if failed_names:
        summary += f" ({', '.join(failed_names)})"
    return {
        "available": True,
        "installed": installed,
        "active": active,
        "failed": failed,
        "failed_names": failed_names,
        "summary": summary,
    }


def _fastembed_cache_present() -> bool:
    """fastembed's own cache resolution (FASTEMBED_CACHE_PATH, else
    ``$TMPDIR/fastembed_cache`` -- matches fastembed's ``define_cache_dir``,
    also relied on by ``frisket.doctor.embeddings``'s own cache probe and
    noted in ``search.py``). The Dockerfile warms both the embedding model
    and the reranker into this one shared root at build time (both load
    through fastembed's ``TextEmbedding`` / ``TextCrossEncoder``, see
    ``Dockerfile``'s pre-download step)."""
    explicit = os.environ.get("FASTEMBED_CACHE_PATH")
    cache_dir = (
        Path(explicit) if explicit else Path(tempfile.gettempdir()) / "fastembed_cache"
    )
    try:
        return cache_dir.is_dir() and any(cache_dir.iterdir())
    except OSError:
        return False


def _rapidocr_model_source() -> str | None:
    """Return the complete default model set's source, if one is offline-ready.

    Runtime resolution uses one root at a time: the installed package first,
    otherwise ``$FRISKET_MODEL_CACHE_DIR/rapidocr``.  Do not treat files split
    across those roots as a complete set; RapidOCR cannot load that union.
    """
    requirements = rapidocr_default_model_requirements()
    if requirements is None:
        return None
    package_root, filenames = requirements
    if rapidocr_model_root_complete(package_root, filenames):
        return "package"
    try:
        from frisket.ai.models.model_cache import default_cache_root

        if rapidocr_model_root_complete(default_cache_root() / "rapidocr", filenames):
            return "shared_cache"
    except Exception:  # noqa: BLE001 -- presence probe, never raises
        pass
    return None


def _rapidocr_models_present() -> bool:
    """Whether one root contains RapidOCR's complete default det/cls/rec set."""
    return _rapidocr_model_source() is not None


def _hf_snapshot_present(
    cache_dir: Path,
    repo_id: str,
    *,
    revision: str | None = None,
    required_files: tuple[str, ...] = (),
) -> bool:
    """Whether a Hugging Face Hub snapshot for ``repo_id`` already sits in
    the persistent hub cache -- the standard ``models--<org>--<repo>/
    snapshots/<revision>`` on-disk layout ``huggingface_hub.snapshot_
    download`` writes (mirrored by ``parakeet_artifacts._validate_
    snapshot``'s own path shape). Filesystem-only: this reads what a prior
    fetch already left behind, it never contacts the Hub itself."""
    try:
        snapshots_dir = (
            cache_dir / f"models--{repo_id.replace('/', '--')}" / "snapshots"
        )
        if not snapshots_dir.is_dir():
            return False
        if revision is not None:
            snapshot = snapshots_dir / revision
            if not snapshot.is_dir():
                return False
            return all((snapshot / f).is_file() for f in required_files)
        # No revision pinned for this repo (an unpinned artifact resolves
        # through the library's own default, not a frisket-pinned revision)
        # -- any prior snapshot is evidence enough.
        return any(
            snap.is_dir() and any(p.is_file() for p in snap.rglob("*"))
            for snap in snapshots_dir.iterdir()
        )
    except OSError:
        return False


def _engine_provisioned(name: str) -> bool:
    """Whether ``name``'s required default weights are already on disk.

    Presence only -- never triggers a download. ``engines_report`` separately
    classifies what an absent set means for each runtime; notably, RapidOCR's
    network-walled worker cannot fetch on first use.
    """
    if name == "embeddings":
        return _fastembed_cache_present()
    if name == "ocr":
        return _rapidocr_models_present()
    if name == "convert":
        # markitdown does format conversion (no ML weights to fetch).
        return True
    if name == "spacy":
        from frisket.ai.models.spacy_model import model_state

        return model_state().status == "present"
    if name in ("parakeet", "faster_whisper"):
        from frisket.engine._workers.parakeet_artifacts import huggingface_hub_cache

        cache_dir = huggingface_hub_cache()
        if name == "faster_whisper":
            # Only the DEFAULT "base" size is probed; model_size is a per-run
            # knob, so the summary carries that caveat next to the label.
            # "base" is now pinned (same hf_snapshot revision-pin mechanism
            # as Parakeet): require the EXACT pinned revision + file list,
            # not just any prior snapshot under this repo id.
            from frisket.ai.models import artifact_manifest

            whisper_entry = artifact_manifest.whisper_base_artifact()
            if whisper_entry is None or whisper_entry.hf_snapshot is None:
                return False
            whisper_snap = whisper_entry.hf_snapshot
            return _hf_snapshot_present(
                cache_dir,
                whisper_snap.repo_id,
                revision=whisper_snap.revision,
                required_files=whisper_snap.files,
            )
        from frisket.engine._workers.parakeet_artifacts import (
            PARAKEET_MODEL_FILES,
            PARAKEET_MODEL_REPO,
            PARAKEET_MODEL_REVISION,
            PARAKEET_VAD_FILES,
            PARAKEET_VAD_REPO,
            PARAKEET_VAD_REVISION,
        )

        # VAD defaults on in ParakeetAdapter,
        # so a real first run needs both snapshots -- require both here too.
        return _hf_snapshot_present(
            cache_dir,
            PARAKEET_MODEL_REPO,
            revision=PARAKEET_MODEL_REVISION,
            required_files=PARAKEET_MODEL_FILES,
        ) and _hf_snapshot_present(
            cache_dir,
            PARAKEET_VAD_REPO,
            revision=PARAKEET_VAD_REVISION,
            required_files=PARAKEET_VAD_FILES,
        )
    return False


def _rapidocr_runtime_report() -> dict[str, Any]:
    """Describe the content-free default RapidOCR execution topology.

    This is configuration/capacity telemetry for the operator-invoked Diagnose
    surface, not a liveness or readiness gate.  It never constructs an engine,
    opens a model, or reports paths, environment values, row data, or OCR text.
    A one-row invocation deliberately narrows to one worker; this report uses
    the multi-row default because that is the capacity relevant to throughput.
    """

    from frisket.engine._workers import rapidocr_session

    override_names = (
        ("workers", rapidocr_session._ENV_WORKERS),
        ("onnx_threads", rapidocr_session._ENV_ONNX_THREADS),
        ("opencv_threads", rapidocr_session._ENV_OPENCV_THREADS),
    )
    overrides = [
        label
        for label, env_name in override_names
        if (os.environ.get(env_name) or "").strip()
    ]
    base: dict[str, Any] = {
        "basis": "multi_row_default",
        "config_mode": "overridden" if overrides else "automatic",
        # Symbolic knob names only. Values are reflected solely through the
        # bounded effective topology below and are never copied from the env.
        "overrides": overrides,
    }
    try:
        topology = rapidocr_session.rapidocr_topology()
    except ValueError as error:
        # The topology parser's bounded errors name only the invalid knob and
        # accepted range; they never echo its raw environment value.
        return {**base, "resolved": False, "error": str(error)}
    except Exception:  # noqa: BLE001 -- INFO probe, never crashes diagnose
        return {
            **base,
            "resolved": False,
            "error": "RapidOCR topology could not be resolved",
        }
    return {
        **base,
        "resolved": True,
        "effective_cpus": topology.effective_cpus,
        "effective_memory_bytes": topology.effective_memory_bytes,
        "workers": topology.workers,
        "onnx_intra_threads": topology.onnx_intra_threads,
        "onnx_inter_threads": topology.onnx_inter_threads,
        "opencv_requested_threads": topology.opencv_threads,
        "memory_limit_mb": topology.memory_mb,
    }


def engines_report() -> dict[str, Any]:
    """Local engine availability, split by what an operator actually needs
    to know before relying on one: an importable package only means the code
    is there. Whether its WEIGHTS are already on disk (fully offline,
    ``provisioned``), can be resolved on first use
    (``fetches_on_first_use``), or leaves an offline-only worker unavailable
    (``offline_unavailable``) is a separate, per-engine fact: offline
    capability belongs to each engine, not to the tier. RapidOCR and Faster
    Whisper belong to the last bucket when their models are absent: their
    trusted workers are network-walled and cannot fetch them mid-run. Never
    fails the health check either way: optional-engine provisioning is
    information, not core application health. The content-free RapidOCR
    capacity/configuration object likewise belongs here (and therefore in
    ``/api/diagnose``), never in ``/api/health``: tuning an optional engine is
    not server liveness.

    Transcription engines are reported under the product ids users pick in a
    run spec (``frisket.sdk.ops.transcribe_engines.ENGINE_CHOICES``:
    ``parakeet``, ``faster_whisper``) — not the package/extra names."""
    installed = []
    for mod, name in (
        ("fastembed", "embeddings"),
        ("rapidocr", "ocr"),
        ("markitdown", "convert"),
        ("onnx_asr", "parakeet"),
        ("faster_whisper", "faster_whisper"),
        ("spacy", "spacy"),
    ):
        try:
            __import__(mod)
            installed.append(name)
        except ImportError:
            pass

    provisioned = []
    fetches_on_first_use = []
    offline_unavailable = []
    for name in installed:
        try:
            ready = _engine_provisioned(name)
        except Exception:  # noqa: BLE001 — presence probe, never crashes the report
            ready = False
        if ready:
            provisioned.append(name)
        elif name in ("ocr", "faster_whisper"):
            # Both trusted workers have an explicit network wall. Missing
            # weights are not deferred downloads; the run will fail until an
            # operator provisions the needed cache.
            offline_unavailable.append(name)
        else:
            fetches_on_first_use.append(name)

    def _label(name: str) -> str:
        if name in offline_unavailable:
            if name == "faster_whisper":
                return (
                    "faster_whisper (not provisioned; pre-populated HF cache required)"
                )
            return f"{name} (not provisioned; offline worker)"
        if name not in provisioned:
            if name == "spacy":
                state = spacy_model_report()["status"]
                if state == "hash_mismatch":
                    return "spacy (model hash mismatch)"
                return "spacy (model not yet downloaded)"
            return f"{name} (fetches on first use)"
        if name == "ocr":
            # The readiness probe covers RapidOCR's default ch/mobile trio.
            # A language override can select a different recognition model.
            return "ocr (provisioned: default det/cls/rec)"
        if name == "faster_whisper":
            # The probe only checks the default "base" size; model_size is
            # per-run, so "provisioned" must not read as size-independent.
            # "base" is pinned + pullable (the manifest entry, same as
            # Parakeet); the no-network worker sandbox requires every other
            # authored size to be present in the Hugging Face cache already.
            return (
                "faster_whisper (provisioned: base pinned & pullable; "
                "other sizes require pre-populated HF cache)"
            )
        return f"{name} (provisioned)"

    summary = (
        ", ".join(_label(name) for name in installed)
        if installed
        else "none installed (repair base install; ASR needs frisket-data[standard])"
    )
    remediation: str | None = None
    needs_provisioning = [*fetches_on_first_use, *offline_unavailable]
    if needs_provisioning:
        steps = ["to provision ahead of a sensitive run:"]
        if "parakeet" in fetches_on_first_use:
            steps.append(
                "pull Parakeet's pinned artifacts with POST "
                "/api/providers/models/pull (the transcribe catalog lists the "
                "pullable refs);"
            )
        if "faster_whisper" in offline_unavailable:
            steps.append(
                "pull the pinned Faster Whisper Base artifact with POST "
                "/api/providers/models/pull, and pre-populate HF_HUB_CACHE "
                "for every needed non-Base size before the no-network worker "
                "sandbox starts;"
            )
        if "ocr" in offline_unavailable:
            steps.append(
                "seed all three default RapidOCR models together under "
                "$FRISKET_MODEL_CACHE_DIR/rapidocr (or warm RapidOCR once with "
                "network access and copy/link that complete set);"
            )
        if "embeddings" in fetches_on_first_use:
            steps.append(
                "pre-populate FastEmbed's FASTEMBED_CACHE_PATH with the "
                "embedding and reranker models;"
            )
        if "spacy" in fetches_on_first_use:
            from frisket.ai.models import artifact_manifest

            steps.append(
                "download the pinned spaCy model from the entities action or "
                "POST /api/providers/models/pull with ref "
                f"{artifact_manifest.SPACY_MODEL_REF}; alternatively run "
                "python -m spacy download en_core_web_sm;"
            )
        known = {"parakeet", "faster_whisper", "ocr", "embeddings", "spacy"}
        if any(name not in known for name in needs_provisioning):
            steps.append("repair the remaining optional runtime and rerun diagnose;")
        remediation = " ".join(steps).removesuffix(";")
    report: dict[str, Any] = {
        "installed": installed,
        "provisioned": provisioned,
        "fetches_on_first_use": fetches_on_first_use,
        "offline_unavailable": offline_unavailable,
        "summary": summary,
        "remediation": remediation,
    }
    if "ocr" in installed:
        report["rapidocr_runtime"] = _rapidocr_runtime_report()
    report["spacy_model"] = spacy_model_report()
    return report


def spacy_model_report() -> dict[str, Any]:
    """Checksum-aware state for the runtime-managed spaCy pipeline.

    This probe never imports spaCy and never reaches the network. It is kept
    separate from the library-presence axis so doctor can still report a
    cached model when the optional Python extra is absent.
    """
    from frisket.ai.models.spacy_model import model_state

    state = model_state()
    report = state.as_dict()
    report["summary"] = {
        "present": f"present ({state.source})",
        "not_downloaded": "not yet downloaded",
        "hash_mismatch": "hash mismatch — download again",
    }[state.status]
    return report


def _command_succeeds(argv: list[str]) -> bool:
    """Run one fixed, local packaging probe without leaking its output."""
    try:
        result = subprocess.run(  # noqa: S603 -- argv is assembled from owned constants
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _pyicu_prerequisite_command() -> str:
    if sys.platform == "darwin":
        return (
            "xcode-select --install; brew install pkg-config icu4c; "
            'export PKG_CONFIG_PATH="$(brew --prefix icu4c)/lib/pkgconfig"'
        )
    if sys.platform.startswith("linux"):
        if shutil.which("apt-get") or shutil.which("apt"):
            return "sudo apt install build-essential python3-dev pkg-config libicu-dev"
        if shutil.which("dnf"):
            return (
                "sudo dnf install gcc-c++ python3-devel libicu-devel pkgconf-pkg-config"
            )
        if shutil.which("pacman"):
            return "sudo pacman -S base-devel python icu pkgconf"
        if shutil.which("apk"):
            return "sudo apk add build-base python3-dev pkgconf icu-dev"
    return "install a C++ compiler, Python headers, pkg-config, and ICU development headers"


def pyicu_install_preflight() -> dict[str, Any]:
    """Offline preflight for the ``entities`` extra's PyICU dependency.

    Frisket's locked PyICU release is sdist-only on PyPI, on every platform.
    An ordinary pip install therefore needs a complete local C++/ICU build
    toolchain. Conda is the supported binary-package path; in particular it is
    the practical Windows path. The result is structured so both the CLI and
    in-product diagnostics can explain exactly which prerequisite is absent.
    """
    try:
        installed_version = importlib.metadata.version("PyICU")
    except importlib.metadata.PackageNotFoundError:
        installed_version = None

    base: dict[str, Any] = {
        "official_pypi_artifacts": "sdist-only",
        "installed_version": installed_version,
    }
    if installed_version is not None:
        return {
            **base,
            "strategy": "installed",
            "ready": True,
            "missing": [],
            "summary": f"PyICU {installed_version} is installed",
            "remediation": None,
        }

    in_conda = bool(
        os.environ.get("CONDA_PREFIX") or os.environ.get("CONDA_DEFAULT_ENV")
    )
    if in_conda:
        remediation = "conda install -c conda-forge pyicu"
        return {
            **base,
            "strategy": "conda-binary",
            "ready": False,
            "missing": ["conda-forge PyICU package"],
            "summary": f"PyICU is absent; in this conda env, run: {remediation}",
            "remediation": remediation,
        }

    if sys.platform.startswith("win"):
        remediation = (
            "create a Miniforge environment, run "
            "`conda install -c conda-forge pyicu`, then install "
            "`frisket-data[entities]` with pip in that environment"
        )
        return {
            **base,
            "strategy": "conda-required",
            "ready": False,
            "missing": ["conda-forge PyICU package"],
            "summary": (
                "PyICU is absent; official PyPI publishes no wheels, and a "
                f"native Windows pip build is unsupported: {remediation}"
            ),
            "remediation": remediation,
        }

    compiler = next(
        (path for name in ("c++", "g++", "clang++") if (path := shutil.which(name))),
        None,
    )
    pkg_config = shutil.which("pkg-config")
    icu_development = bool(
        pkg_config and _command_succeeds([pkg_config, "--exists", "icu-i18n"])
    )
    include_dir = sysconfig.get_path("include")
    python_headers = bool(include_dir and (Path(include_dir) / "Python.h").is_file())
    missing = [
        label
        for present, label in (
            (compiler, "C++ compiler"),
            (python_headers, "Python development headers"),
            (pkg_config, "pkg-config"),
            (icu_development, "ICU development headers/libraries"),
        )
        if not present
    ]
    remediation = None if not missing else _pyicu_prerequisite_command()
    summary = (
        "PyICU source-build prerequisites detected (compiler, Python headers, "
        "pkg-config, ICU)"
        if not missing
        else f"PyICU source-build prerequisites missing: {', '.join(missing)}; run: {remediation}"
    )
    return {
        **base,
        "strategy": "source-build",
        "ready": not missing,
        "compiler": compiler,
        "python_headers": python_headers,
        "pkg_config": pkg_config,
        "icu_development": icu_development,
        "missing": missing,
        "summary": summary,
        "remediation": remediation,
    }


def entities_report() -> dict[str, Any]:
    """Real import-and-catch probe for the ``entities`` extra
    (followthemoney/normality/pyicu) -- the same idiom ``engines_report()`` uses
    (an actual import attempt, not just a presence check), so a
    discoverable-but-broken install (missing/broken normality, rigour, or the
    native pyicu extension) is honestly reported as unavailable instead of
    raising through this probe. A missing/broken extra is optional-and-honest,
    not an error: base
    install/doctor stay green without it."""
    try:
        from frisket.features.followthemoney import entities_available

        ok, err = entities_available()
    except Exception as exc:  # noqa: BLE001 -- report, never raise
        ok, err = (
            False,
            (
                "FollowTheMoney entity support failed to load "
                f"({exc}). Install with pip install 'frisket-data[entities]'."
            ),
        )
    preflight = pyicu_install_preflight()
    summary = "followthemoney (entities extra) installed" if ok else err
    if not ok:
        summary = (
            f"{err} PyICU is sdist-only on PyPI (no official wheels on any "
            f"platform). {preflight['summary']}."
        )
    return {
        "installed": ok,
        "summary": summary,
        "pyicu_install_preflight": preflight,
    }


def models_sidecar_report() -> dict[str, Any]:
    """Probe the frisket-models sidecar's GET /capabilities when
    FRISKET_MODELS_URL is set. Absent env is a quiet skip (the local tier is
    fully healthy without it), same as an ImportError for local engines.

    Shares its probe with server/app.py's action-catalog discovery via
    ``ops._sidecar.probe_sidecar_capabilities`` (rule-of-three consolidation);
    this wrapper only adds the human-readable ``summary`` line and folds
    app.py's token-missing/unreachable distinction into one generic
    "unavailable" bucket, which is all this surface ever showed."""
    from frisket.ops._sidecar import (
        probe_sidecar_capabilities,
        sidecar_base_url,
        sidecar_token,
    )

    base = sidecar_base_url()
    if not base:
        return {
            "configured": False,
            "available": False,
            "engines": [],
            "error": None,
            "summary": "FRISKET_MODELS_URL unset (sidecar engines disabled)",
        }
    result = probe_sidecar_capabilities(
        base=base,
        token=sidecar_token(),
        timeout=5.0,
        format_error=str,
    )
    engines_ = result["engines"]
    if result["available"]:
        ver = result.get("version", "?")
        summary = (
            f"{base} reachable but advertises no engines"
            if not engines_
            else f"{base} (v{ver}): "
            + ", ".join(
                f"{e['name']}={'up' if e.get('available') else 'down'}"
                for e in engines_
            )
        )
    else:
        summary = f"{base} unavailable ({result['error']})"
    return {**result, "summary": summary}


def media_toolbelt_report() -> dict[str, Any]:
    """Optional media binaries, reported as INFO.

    ``deno`` backs yt-dlp's JS challenge solving. Absent, YouTube extraction
    falls back to jsless clients and returns fewer formats while still exiting
    zero; yt-dlp's own warning about that is suppressed because the download
    child runs with ``--no-warnings``, so this probe is the only signal.
    """
    tools = ("ffmpeg", "pdftoppm", "deno")
    present = [t for t in tools if shutil.which(t)]
    missing = [t for t in tools if t not in set(present)]
    summary = ", ".join(present) or "none on PATH"
    if missing:
        summary = f"{summary} (missing: {', '.join(missing)})"
    return {"present": present, "missing": missing, "summary": summary}


def ytdlp_update_report(
    fetch_latest: Any | None = None,
) -> dict[str, Any]:
    """On-demand yt-dlp version check against PyPI (INFO; network).

    yt-dlp ages fast (site extractors break upstream), but the package only
    changes when the installation does — so the honest remediation is
    "update this Frisket installation", never an in-app action. This is the
    one probe that leaves the deployment (pypi.org); it runs only when the
    operator explicitly invokes doctor//api/diagnose, never ambiently.

    ``fetch_latest`` is a test seam returning the latest version string.
    """

    def _numeric(value: str | None) -> tuple[int, ...] | None:
        # Stable yt-dlp releases use numeric calendar versions; anything else
        # (dev/rc installs) is honestly "unknown", not a fabricated compare.
        if value is None or re.fullmatch(r"\d+(?:\.\d+){2,3}", value) is None:
            return None
        return tuple(int(part) for part in value.split("."))

    try:
        installed: str | None = importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        installed = None
    if installed is None:
        return {
            "installed": None,
            "latest": None,
            "update_available": None,
            "summary": "yt-dlp is not installed",
        }

    try:
        if fetch_latest is not None:
            latest = fetch_latest()
        else:
            import httpx

            resp = httpx.get(
                "https://pypi.org/pypi/yt-dlp/json",
                headers={"accept": "application/json"},
                timeout=3.0,
            )
            resp.raise_for_status()
            latest = resp.json()["info"]["version"]
    except Exception as e:  # noqa: BLE001 — reachability probe, never raises
        return {
            "installed": installed,
            "latest": None,
            "update_available": None,
            "summary": f"yt-dlp {installed} installed; PyPI unreachable ({e})",
        }

    installed_key = _numeric(installed)
    latest_key = _numeric(latest if isinstance(latest, str) else None)
    if installed_key is None or latest_key is None:
        return {
            "installed": installed,
            "latest": latest if isinstance(latest, str) else None,
            "update_available": None,
            "summary": f"yt-dlp {installed} installed; cannot compare with PyPI ({latest!r})",
        }
    if latest_key > installed_key:
        return {
            "installed": installed,
            "latest": latest,
            "update_available": True,
            "summary": (
                f"yt-dlp {installed} installed, {latest} on PyPI — update this "
                "Frisket installation (and restart it) to pick it up"
            ),
        }
    return {
        "installed": installed,
        "latest": latest,
        "update_available": False,
        "summary": f"yt-dlp {installed} is current",
    }


def static_assets_report() -> dict[str, Any]:
    """Reuses `frisket.server.static_serving`'s resolution order (explicit
    dir/env -> packaged wheel bundle -> dev mode) so this probe and the
    server's actual mount decision (server/app.py's `create_app`) can never
    disagree about what's serving the UI."""
    from frisket.server.static_serving import describe_static_source, resolve_static_dir

    resolved = resolve_static_dir()
    return {
        "configured": str(resolved) if resolved is not None else None,
        "summary": describe_static_source(),
    }


def _info_probe(label: str, fn: Any) -> dict[str, Any]:
    """Each INFO probe is independently guarded so one broken probe (e.g. a
    partial/broken optional extra) cannot 500 the whole `/api/diagnose`
    response. Mirrors `frisket/cli.py`'s `doctor()` printer, which already
    wraps each INFO line in its own try/except -- this gives the JSON route
    the same guarantee."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 — INFO probes report, never crash the caller
        return {"ok": False, "error": str(e), "summary": f"{label} unavailable ({e})"}


def run_diagnostics(
    *,
    router: Any | None = None,
    queue: Any | None = None,
    liveness_window_seconds: float = 90.0,
    queue_timeout_seconds: float | None = None,
    workspace_root: str | Path | None = None,
    project: Any | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """The single aggregate report `frisket doctor` prints and the
    `/api/diagnose` route serves as JSON. CORE probe failures never raise
    here — they flip ``healthy`` to False so an in-app panel can render a
    partial report instead of a 500.

    ``queue``/``liveness_window_seconds``/``queue_timeout_seconds`` and
    ``workspace_root`` and ``project``/``project_id`` are all optional
    (decision 9 additions): bare ``frisket doctor`` calls this with none of
    them and gets honest skips (same idiom as ``models_sidecar_report``'s
    absent-env skip); the ``/api/diagnose`` route passes the real workspace
    state through.
    """
    core: dict[str, Any] = {}
    try:
        core["project_store"] = {"ok": True, **store_roundtrip_report()}
    except Exception as e:  # noqa: BLE001 — report, never crash the caller
        core["project_store"] = {"ok": False, "error": str(e)}

    info = {
        "python": _info_probe("python", python_version_report),
        "model_providers": _info_probe(
            "model providers", lambda: provider_report(router)
        ),
        "local_model_endpoints": _info_probe(
            "local model endpoints",
            lambda: local_model_endpoints_report(router=router),
        ),
        "provider_key_validation": _info_probe(
            "provider key validation", lambda: provider_key_validation_report(router)
        ),
        "replay_mode": _info_probe("replay mode", lambda: replay_mode_report(router)),
        "queue_health": _info_probe(
            "queue health",
            lambda: queue_health_report(
                queue,
                liveness_window_seconds=liveness_window_seconds,
                timeout_seconds=queue_timeout_seconds,
            ),
        ),
        "disk_free": _info_probe("disk free", lambda: disk_free_report(workspace_root)),
        "plugin_health": _info_probe(
            "plugin health",
            lambda: plugin_health_report(project, project_id=project_id),
        ),
        "local_engines": _info_probe("local engines", engines_report),
        "spacy_model": _info_probe("spaCy model", spacy_model_report),
        "entities_extra": _info_probe("entities extra", entities_report),
        "models_sidecar": _info_probe("models sidecar", models_sidecar_report),
        "media_toolbelt": _info_probe("media toolbelt", media_toolbelt_report),
        "ytdlp_update": _info_probe("yt-dlp update", ytdlp_update_report),
        "static_assets": _info_probe("static assets", static_assets_report),
    }
    healthy = all(c.get("ok") for c in core.values())
    return {"healthy": healthy, "core": core, "info": info}
