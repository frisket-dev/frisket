from __future__ import annotations

import dataclasses
import importlib
import inspect
import re
from pathlib import Path
from typing import Any

import pytest
from frisket.execution.attempt_authority import UnroutedOnlyAuthority

pytestmark = pytest.mark.gap

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
RECORD = "the public/private package boundary"

# The open trees that must be private-free after Stage 1B.
OPEN_TREES = ("jobs", "runner", "executor", "server", "team")

# The private package today, plus the names a future private split may use.

# Port ROLES, matched against field/parameter names. Names are the
# implementer's; these regexes describe the CAPABILITY each port carries.
CREDENTIAL_RE = re.compile(
    r"(?i)(credential|secret|byok|provider_key|key_access|keyring|keys_(port|source|resolver|provider))"
)
ADMISSION_RE = re.compile(
    r"(?i)(admission|admit|dispatch|runtime|execution|sandbox|deny)"
)
SETTLEMENT_RE = re.compile(
    r"(?i)(settlement|billing|outcome|charge|reconcil|meter|usage|ledger)"
)
# Names that would otherwise trip ADMISSION_RE ("notification_delivery_runtime"
# is not an admission port).
ROLE_EXCLUDE_RE = re.compile(r"(?i)(notification|delivery|logging|log_)")

ROLES: dict[str, re.Pattern[str]] = {
    "credential": CREDENTIAL_RE,
    "admission": ADMISSION_RE,
    "settlement": SETTLEMENT_RE,
}

BYOK_SECRET = "sk-open-team-byok-plaintext"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _role_of(name: str) -> str | None:
    if ROLE_EXCLUDE_RE.search(name):
        return None
    for role, pattern in ROLES.items():
        if pattern.search(name):
            return role
    return None


def _python_sources(tree: Path) -> list[Path]:
    return [p for p in sorted(tree.rglob("*.py")) if "__pycache__" not in p.parts]


def _jobs_modules() -> list[Any]:
    """Import every module under src/frisket/engine/jobs (they import today)."""
    mods = []
    failures = []
    for path in _python_sources(SRC / "frisket" / "engine" / "jobs"):
        rel = path.relative_to(SRC).with_suffix("")
        parts = [p for p in rel.parts if p != "__init__"]
        name = ".".join(parts)
        try:
            mods.append(importlib.import_module(name))
        except Exception as exc:  # noqa: BLE001 — reported as an assertion below
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    assert not failures, f"frisket.engine.jobs modules failed to import: {failures}"
    return mods


def _fields_of(obj: Any) -> dict[str, Any]:
    """Role-bearing field/parameter surface of a class or callable.

    Returns {name: default-or-inspect.Parameter.empty}. Covers dataclasses,
    Protocols/annotation-only carriers, __init__ signatures, and plain
    callables (registration functions taking port keyword arguments).
    """
    fields: dict[str, Any] = {}
    if inspect.isclass(obj):
        if dataclasses.is_dataclass(obj):
            for f in dataclasses.fields(obj):
                if f.default is not dataclasses.MISSING:
                    fields[f.name] = f.default
                elif f.default_factory is not dataclasses.MISSING:  # type: ignore[misc]
                    try:
                        fields[f.name] = f.default_factory()  # type: ignore[misc]
                    except Exception:  # noqa: BLE001
                        fields[f.name] = inspect.Parameter.empty
                else:
                    fields[f.name] = inspect.Parameter.empty
        for name, annotation in getattr(obj, "__annotations__", {}).items():
            fields.setdefault(name, getattr(obj, name, inspect.Parameter.empty))
            if fields[name] is inspect.Parameter.empty and _optional_annotation(
                annotation
            ):
                fields[name] = None
        try:
            sig = inspect.signature(obj.__init__)
        except (TypeError, ValueError):
            sig = None
        if sig is not None:
            for name, param in sig.parameters.items():
                if name == "self" or param.kind in (
                    inspect.Parameter.VAR_POSITIONAL,
                    inspect.Parameter.VAR_KEYWORD,
                ):
                    continue
                fields.setdefault(name, param.default)
    elif callable(obj):
        try:
            sig = inspect.signature(obj)
        except (TypeError, ValueError):
            return {}
        for name, param in sig.parameters.items():
            if param.kind in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD,
            ):
                continue
            fields[name] = param.default
    return fields


def _optional_annotation(annotation: Any) -> bool:
    text = annotation if isinstance(annotation, str) else str(annotation)
    return "None" in text or "Optional" in text


def _roles_covered(fields: dict[str, Any]) -> dict[str, str]:
    """{role: field name} for the roles this surface covers."""
    covered: dict[str, str] = {}
    for name in fields:
        role = _role_of(name)
        if role is not None:
            covered.setdefault(role, name)
    return covered


def _port_carriers() -> list[tuple[str, Any, dict[str, Any], dict[str, str]]]:
    """Every surface in frisket.jobs covering ALL THREE port roles."""
    carriers = []
    for module in _jobs_modules():
        for attr, obj in vars(module).items():
            if attr.startswith("_"):
                continue
            if getattr(obj, "__module__", None) != module.__name__:
                continue
            if not (inspect.isclass(obj) or callable(obj)):
                continue
            fields = _fields_of(obj)
            covered = _roles_covered(fields)
            if len(covered) == len(ROLES):
                carriers.append((f"{module.__name__}.{attr}", obj, fields, covered))
    return carriers


def _open_registration() -> tuple[str, Any]:
    """The OPEN worker handler-registration entrypoint (frisket.jobs.worker):
    a public callable taking the handler registry plus a workspace root, whose
    name is not hosted/cloud-flavoured."""
    module = importlib.import_module("frisket.engine.jobs.worker")
    candidates = []
    for attr, obj in vars(module).items():
        if attr.startswith("_") or not inspect.isfunction(obj):
            continue
        if getattr(obj, "__module__", None) != module.__name__:
            continue
        if re.search(r"(?i)(hosted|cloud)", attr):
            continue
        params = _fields_of(obj)
        if "registry" in params and "workspace_root" in params:
            candidates.append((f"frisket.engine.jobs.worker.{attr}", obj))
    assert candidates, (
        "frisket.engine.jobs.worker exposes no OPEN handler-registration entrypoint"
        " (a public callable taking `registry` + `workspace_root`); the open"
        " worker must be composable without any hosted/cloud registration"
    )
    # Prefer the broadest product registration (most parameters).
    candidates.sort(key=lambda item: len(_fields_of(item[1])), reverse=True)
    return candidates[0]


def _call_filtered(fn: Any, **kwargs: Any) -> Any:
    """Call fn with only the keyword arguments it accepts (port names are the
    implementer's; we never force an unknown one)."""
    accepted = _fields_of(fn)
    return fn(**{k: v for k, v in kwargs.items() if k in accepted})


def _make_template_run(workspace: Path, name: str = "queued"):
    """A network-free template run (same shape as tests/test_worker.py)."""
    from frisket.ai.llm import ModelRouter
    from frisket.engine.runner import MapRunner
    from frisket.engine.store import Project

    project = Project.create(workspace / f"{name}.frisket", name=name)
    sheet = project.add_sheet("people")
    cols = {
        "first": project.add_column(sheet, "first"),
        "last": project.add_column(sheet, "last"),
    }
    project.add_rows(
        sheet,
        [
            {"first": "Ada", "last": "Lovelace"},
            {"first": "Grace", "last": "Hopper"},
        ],
        cols,
    )
    spec = {
        "action_kind": "map.template",
        "sheet_id": sheet,
        "input_columns": ["first", "last"],
        "template": "{{last}}, {{first}}",
        "output_name": "display",
    }
    progress = MapRunner(
        project,
        ModelRouter(cache=None, cache_mode="off"),
        authority=UnroutedOnlyAuthority(project),
    ).prepare_run(spec, confirmed=True)
    project.close()
    return name, progress.run_id, spec


# ---------------------------------------------------------------------------
# 1. The central red: open code must not import the private package
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 2. Explicit ports, discovered by capability
# ---------------------------------------------------------------------------


def test_worker_declares_explicit_credential_admission_settlement_ports() -> None:
    carriers = _port_carriers()
    assert carriers, (
        "no surface in frisket.jobs covers all three worker port roles"
        f" ({sorted(ROLES)}). The worker must declare EXPLICIT ports — a"
        " credential/secret-access port, a runtime/admission port, and an"
        " outcome/settlement port — as constructor/registration parameters or a"
        " port carrier (dataclass/protocol/registry). Monkey-patching and"
        f" import-time side effects are rejected ({RECORD})"
    )


def test_settlement_port_requires_the_trusted_job_org_fact() -> None:
    """The port may be absent; the funding identity at an effect site may not."""
    from frisket.engine.jobs.ports import SettlementPort

    signature = inspect.signature(SettlementPort.settle_run)
    parameter = signature.parameters.get("trusted_job_org_id")

    assert parameter is not None, signature
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY, signature
    assert parameter.default is inspect.Parameter.empty, signature
    assert "TrustedJobOrg" in str(parameter.annotation), parameter.annotation


def test_ports_are_injectable_through_the_open_worker_registration() -> None:
    """Constructor injection, not monkey-patching: the open registration
    entrypoint must ACCEPT the ports (directly, or via a port carrier
    parameter)."""
    name, fn = _open_registration()
    params = _fields_of(fn)
    covered = _roles_covered(params)
    carrier_param = [
        p
        for p in params
        if re.search(r"(?i)(port|adapter|edition|seam)", p)
        and not ROLE_EXCLUDE_RE.search(p)
    ]
    assert covered or carrier_param, (
        f"{name} accepts no port arguments (params={sorted(params)}) — the open"
        " worker cannot be composed with explicit credential/admission/"
        " settlement ports, so the private behaviour can only arrive by import"
        f" or monkey-patch ({RECORD})"
    )
    if carrier_param:
        return
    assert len(covered) == len(ROLES), (
        f"{name} exposes only {sorted(covered)} of the required port roles"
        f" {sorted(ROLES)}; params={sorted(params)}"
    )


# ---------------------------------------------------------------------------
# 3. OPEN control-plane access: BYOK run with the private package unimportable
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 4. Private ports still wired for cloud (billing reconcile + code denial)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. Fail closed
# ---------------------------------------------------------------------------


def test_unknown_port_implementation_is_refused(tmp_path) -> None:
    """An object that does not satisfy a port contract must be REFUSED (raise),
    never silently ignored or silently permitted."""
    from frisket.engine.jobs import SqliteJobQueue, default_registry

    reg_name, register = _open_registration()
    params = _fields_of(register)
    port_params = [p for p in params if _role_of(p) is not None] + [
        p
        for p in params
        if re.search(r"(?i)(port|adapter)", p) and not ROLE_EXCLUDE_RE.search(p)
    ]
    assert port_params, (
        f"{reg_name} declares no port parameters — see"
        " test_ports_are_injectable_through_the_open_worker_registration"
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    queue = SqliteJobQueue(workspace / ".queue.db")

    class NotAPort:
        """Satisfies no port protocol."""

    try:
        for port in port_params:
            kwargs = {
                "registry": default_registry(),
                "workspace_root": workspace,
                "queue": queue,
                port: NotAPort(),
            }
            with pytest.raises((TypeError, ValueError)):
                _call_filtered(register, **kwargs)
    finally:
        queue.close()
