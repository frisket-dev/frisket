"""`frisket doctor` — the deployment self-probe.

Without a subcommand it runs the local project-store/search round-trip probe;
`frisket doctor embeddings [--json]` reports the embedding product path.
"""

import json
import os
import sys
from pathlib import Path


DOCTOR_USAGE = """Usage:
  frisket doctor
  frisket doctor embeddings [--json]

Without a subcommand, runs the local project-store/search self-probe.
`frisket doctor embeddings` reports the embedding product path (fastembed +
model cache, provider key PRESENCE by name, sidecar embedding-route health,
scheduler config, and live-proof skip/run status); --json prints the raw report.
"""


def doctor(argv: list[str] | None = None) -> int:
    """Self-probe: exercise the core machinery and report. Exit 0 = healthy.

    CORE probes (failure = unhealthy, exit 1): the project store actually
    round-trips data (create bundle -> write -> read back), and the search
    sidecar indexes it. INFO probes (reported, never fail the doctor): model
    providers/keys, local engines, media toolbelt — the local tier is fully
    healthy keyless, so their absence is a note, not a failure.
    """
    argv = [] if argv is None else argv
    if argv and argv[0] in {"-h", "--help"}:
        print(DOCTOR_USAGE)
        return 0
    if argv and argv[0] == "embeddings":
        return _embeddings_doctor(argv[1:])
    if argv:
        print("unknown doctor subcommand")
        return 2

    from frisket.operability import diagnostics

    failures = 0

    def core(label: str, fn) -> None:
        nonlocal failures
        try:
            detail = fn() or "ok"
            print(f"  ✓ {label}: {detail}")
        except Exception as e:  # noqa: BLE001 — doctor reports, never crashes
            failures += 1
            print(f"  ✗ {label}: {e}")

    def info(label: str, fn) -> None:
        try:
            print(f"  · {label}: {fn()}")
        except Exception as e:  # noqa: BLE001
            print(f"  · {label}: unavailable ({e})")

    def _engines_summary() -> str:
        r = diagnostics.engines_report()
        if r.get("remediation"):
            return f"{r['summary']}\n      ↳ {r['remediation']}"
        return r["summary"]

    def _local_endpoints_summary() -> str:
        report = diagnostics.local_model_endpoints_report()
        if report["configured"] == 0:
            return "none configured"
        return f"{report['reachable']} of {report['configured']} endpoints reachable"

    print("frisket doctor")
    print("CORE")
    core("python", lambda: diagnostics.python_version_report()["version"])
    core(
        "project store + search", lambda: diagnostics.store_roundtrip_report()["detail"]
    )

    print("INFO")
    info("model providers", lambda: diagnostics.provider_report()["summary"])
    info("local model endpoints", _local_endpoints_summary)
    info(
        "provider key validation",
        lambda: diagnostics.provider_key_validation_report()["summary"],
    )
    info("replay mode", lambda: diagnostics.replay_mode_report()["summary"])
    info("local engines", _engines_summary)
    info("spaCy model", lambda: diagnostics.spacy_model_report()["summary"])
    info("entities extra", lambda: diagnostics.entities_report()["summary"])
    info("models sidecar", lambda: diagnostics.models_sidecar_report()["summary"])
    info("media toolbelt", lambda: diagnostics.media_toolbelt_report()["summary"])
    info("yt-dlp update", lambda: diagnostics.ytdlp_update_report()["summary"])
    info("static assets", lambda: diagnostics.static_assets_report()["summary"])
    info("disk free", lambda: diagnostics.disk_free_report()["summary"])
    info(
        "queue health",
        lambda: diagnostics.queue_health_report()["summary"],
    )
    info(
        "plugin health",
        lambda: diagnostics.plugin_health_report()["summary"],
    )

    print(
        f"doctor: {'HEALTHY' if failures == 0 else f'UNHEALTHY ({failures} core failure(s))'}"
    )
    return 0 if failures == 0 else 1


def _embeddings_doctor(argv: list[str]) -> int:
    """`frisket doctor embeddings [--json]` — deterministic embeddings live-proof
    harness (Lane E). Reports, never fails: missing config is INFO. Uses the real
    ModelRouter (by NAME only, no key values) and the named workspace when one is
    discoverable. --json prints the raw structured report."""
    as_json = False
    rest = []
    for a in argv:
        if a == "--json":
            as_json = True
        elif a in {"-h", "--help"}:
            print("Usage: frisket doctor embeddings [--json]")
            return 0
        else:
            rest.append(a)
    if rest:
        print(f"unexpected argument(s): {' '.join(rest)}", file=sys.stderr)
        return 2

    from frisket.operability.doctor import report_embeddings

    router = None
    try:
        from frisket.ai.llm import ModelRouter

        router = ModelRouter()
    except Exception:  # noqa: BLE001 — doctor reports, never crashes
        router = None

    workspace_root = _embeddings_doctor_workspace()
    report = report_embeddings(router=router, workspace_root=workspace_root)

    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0

    _print_embeddings_report(report)
    return 0


def _embeddings_doctor_workspace() -> Path | None:
    """The workspace to count scheduled indexes over: FRISKET_DATA_DIR/projects
    when hosted, else ./frisket-projects if it exists. None means skip the count
    (no fabricated default that would scan an unrelated directory)."""
    data_dir = os.environ.get("FRISKET_DATA_DIR")
    if data_dir:
        projects = Path(data_dir) / "projects"
        if projects.is_dir():
            return projects
    local = Path.cwd() / "frisket-projects"
    return local if local.is_dir() else None


def _print_embeddings_report(report: dict) -> None:
    print("frisket doctor embeddings")

    fe = report.get("fastembed", {})
    state = (
        "active"
        if fe.get("active")
        else (
            "disabled (FRISKET_DISABLE_LOCAL_EMBED=1)"
            if fe.get("disabled_by_env")
            else "missing from base install (reinstall Frisket)"
        )
    )
    print("fastembed")
    print(f"  · status: {state}")
    print(f"  · local model: {fe.get('local_model_id')}")
    cache = fe.get("cache", {})
    size = cache.get("approx_bytes")
    size_note = f"~{size} bytes" if isinstance(size, int) else "n/a"
    present = "present" if cache.get("exists") else "absent"
    print(f"  · model cache: {cache.get('path')} ({present}, {size_note})")

    print("providers (key PRESENCE by name only — values never read)")
    for name, info_ in report.get("providers", {}).items():
        flag = "key present" if info_.get("key_present") else "no key"
        print(f"  · {name}: {flag} (env {info_.get('key_env')})")

    sidecar = report.get("sidecar", {})
    print("sidecar")
    if not sidecar.get("configured"):
        print(f"  · {sidecar.get('detail', 'unconfigured')}")
    else:
        line = f"  · {sidecar.get('url')}: {sidecar.get('status')}"
        if sidecar.get("status") == "reachable":
            engs = sidecar.get("engines") or []
            parts = [
                f"{e.get('name')}={'up' if e.get('available') else 'down'}"
                for e in engs
            ]
            line += f" (v{sidecar.get('version', '?')}): " + (
                ", ".join(parts) if parts else "no engines advertised"
            )
        elif sidecar.get("detail"):
            line += f" ({sidecar['detail']})"
        print(line)

    sched = report.get("scheduler", {})
    print("scheduler")
    print(f"  · interval: {sched.get('interval_seconds')} ({sched.get('status')})")
    print(f"  · scheduled indexes: {sched.get('scheduled_indexes')}")

    print("live proofs (optional / key-gated — would_skip is NOT a failure)")
    for name, proof in report.get("live_proofs", {}).items():
        print(f"  · {name}: {proof.get('status')} — {proof.get('reason')}")
