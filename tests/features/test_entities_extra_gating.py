"""Followthemoney moved from a base dependency to the `entities` extra. These checks pin the
graceful-degradation contract via monkeypatch (so they run identically
whether or not the extra actually happens to be installed in this env) —
every listed import site must fail closed with the remediation string
`pip install 'frisket[entities]'`, never a bare traceback.

Adversarial cases cover direct-submodule imports, native FtM action failures,
a discoverable-but-broken-install simulation, and per-probe diagnose
protection.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

import pytest

from frisket.actions.core import RegisteredAction
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import ActionRequest
from frisket.engine.executor.callable_action import run_typed_callable_action
from frisket.engine.store import Project
from frisket.features.graph import neighborhood
from frisket.operability import diagnostics

REMEDIATION = "pip install 'frisket[entities]'"
ROOT = Path(__file__).resolve().parents[2]
FTM_PLUGIN_ROOT = (
    ROOT / "src" / "frisket" / "authoring" / "bundled_plugins" / "frisket.ftm"
)


def test_entities_available_reflects_the_real_availability_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import frisket.features.followthemoney as ftm

    monkeypatch.setattr(ftm, "ENTITIES_AVAILABLE", True)
    ok, err = ftm.entities_available()
    assert ok is True
    assert err is None

    monkeypatch.setattr(ftm, "ENTITIES_AVAILABLE", False)
    ok, err = ftm.entities_available()
    assert ok is False
    assert err is not None
    assert REMEDIATION in err


def test_graph_neighborhood_gates_gracefully_when_entities_extra_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        neighborhood,
        "entities_available",
        lambda: (
            False,
            f"FollowTheMoney entity support is not installed. Install with {REMEDIATION}.",
        ),
    )
    with tempfile.TemporaryDirectory() as td:
        project = Project.create(Path(td) / "gate.frisket", name="gate")
        try:
            with pytest.raises(neighborhood.GraphNeighborhoodError) as excinfo:
                neighborhood.build_graph_neighborhood(project, anchor_id="anything")
        finally:
            project.close()
    assert excinfo.value.code == "entities_extra_missing"
    assert excinfo.value.status_code == 503
    assert REMEDIATION in excinfo.value.message


def test_graph_neighborhood_route_returns_503_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from frisket.server.app import create_app

    monkeypatch.setattr(
        neighborhood,
        "entities_available",
        lambda: (
            False,
            f"FollowTheMoney entity support is not installed. Install with {REMEDIATION}.",
        ),
    )
    with tempfile.TemporaryDirectory() as td:
        app = create_app(td)
        client = TestClient(app, raise_server_exceptions=False)
        create_resp = client.post("/api/projects", json={"name": "gate-http"})
        assert create_resp.status_code == 200, create_resp.text
        resp = client.get(
            "/api/projects/gate-http/graph/neighborhood",
            params={"anchor_id": "anything"},
        )
        assert resp.status_code == 503, resp.text
        body = resp.json()
        assert body["detail"]["code"] == "entities_extra_missing"
        assert REMEDIATION in body["detail"]["message"]


def test_doctor_entities_probe_stays_info_never_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`entities_report()` is an INFO probe (diagnostics.run_diagnostics): a
    missing extra must never flip `healthy` to False."""
    import frisket.features.followthemoney as ftm

    monkeypatch.setattr(ftm, "ENTITIES_AVAILABLE", False)
    report = diagnostics.entities_report()
    assert report["installed"] is False
    assert REMEDIATION in report["summary"]

    full = diagnostics.run_diagnostics()
    assert full["info"]["entities_extra"]["installed"] is False
    # CORE probes alone decide healthy; entities is INFO-only.
    assert full["healthy"] is all(c.get("ok") for c in full["core"].values())


def test_diagnose_info_probes_are_individually_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One broken INFO probe must not crash
    `run_diagnostics()` (and therefore `/api/diagnose`). Simulate
    `entities_report` itself blowing up with something other than the
    handled case, and confirm the aggregate call still returns cleanly with
    every OTHER probe intact."""

    def _boom() -> dict[str, object]:
        raise RuntimeError("simulated probe crash")

    monkeypatch.setattr(diagnostics, "entities_report", _boom)
    full = diagnostics.run_diagnostics()
    assert full["info"]["entities_extra"]["ok"] is False
    assert "simulated probe crash" in full["info"]["entities_extra"]["error"]
    # Every other probe still ran normally.
    assert "summary" in full["info"]["local_engines"]
    assert "summary" in full["info"]["media_toolbelt"]


def test_ftm_migration_harness_gates_gracefully_when_entities_extra_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import frisket.features.followthemoney.migration_harness as harness

    monkeypatch.setattr(
        harness,
        "entities_available",
        lambda: (
            False,
            f"FollowTheMoney entity support is not installed. Install with {REMEDIATION}.",
        ),
    )
    with pytest.raises(RuntimeError) as excinfo:
        harness._require_entities()
    assert REMEDIATION in str(excinfo.value)


@pytest.mark.parametrize(
    ("definition_name", "params"),
    [
        ("FTM_IMPORT", {"source_path": "not-read.jsonl"}),
        (
            "FTM_EXPORT",
            {"rowsets": [{"sheet_id": 1}], "mappings": [{"schema": "Person"}]},
        ),
    ],
)
def test_native_ftm_actions_name_missing_extra_remediation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    definition_name: str,
    params: dict[str, object],
) -> None:
    """The ordinary callable host preserves actionable FtM extra failures."""
    from frisket.engine.executor import entity_package

    monkeypatch.setattr(
        entity_package,
        "entities_available",
        lambda: (False, f"Install with {REMEDIATION}."),
    )
    spec = importlib.util.spec_from_file_location(
        "_test_native_ftm_actions", FTM_PLUGIN_ROOT / "ftm_actions.py"
    )
    assert spec is not None and spec.loader is not None
    native = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(native)
    definition = getattr(native, definition_name)
    registered = RegisteredAction(f"frisket.ftm.{definition.name}", definition)
    request = BoundTypedActionRequest.bind(
        registered,
        ActionRequest(
            action_id=registered.action_id,
            scope={"kind": "project"},
            params=params,
            idempotency_key=definition.name,
        ),
    )
    with closing(Project.create(tmp_path / "project", name="FtM")) as project:
        result = run_typed_callable_action(project, "p", request)

    assert result.status == "failed"
    assert result.errors[0].code == "followthemoney_unavailable"
    assert REMEDIATION in result.errors[0].message


# --------------------------------------------------------------------------- #
# Direct submodule imports, not just the
# handful of known external call sites.
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(
    importlib.util.find_spec("followthemoney") is not None,
    reason="asserts the no-extra direct-import contract; run without followthemoney installed",
)
@pytest.mark.parametrize(
    "module_name",
    [
        "frisket.features.followthemoney.adapter",
        "frisket.features.followthemoney.import_planner",
        "frisket.features.followthemoney.mapping",
        "frisket.features.followthemoney.schema_catalog",
        "frisket.features.followthemoney.exporters",
    ],
)
def test_direct_submodule_import_gates_gracefully_no_extra(module_name: str) -> None:
    """Every frisket.features.followthemoney submodule that hard-imports the SDK must
    raise a clean, actionable ImportError on a DIRECT import -- not a bare
    ModuleNotFoundError, and not only when reached through the handful of
    known external call sites (graph/neighborhood.py, the bundled plugin)."""
    sys.modules.pop(module_name, None)
    with pytest.raises(ImportError) as excinfo:
        importlib.import_module(module_name)
    assert REMEDIATION in str(excinfo.value)


@pytest.mark.skipif(
    importlib.util.find_spec("followthemoney") is not None,
    reason="asserts the no-extra re-export contract; run without followthemoney installed",
)
def test_missing_public_reexport_gates_gracefully_no_extra() -> None:
    """`from frisket.features.followthemoney import validate_entity` (or any other
    normally-real-exported name) must also raise a clean ImportError, not
    Python's generic "cannot import name"."""
    with pytest.raises(ImportError) as excinfo:
        from frisket.features.followthemoney import validate_entity  # noqa: F401

    assert REMEDIATION in str(excinfo.value)


# --------------------------------------------------------------------------- #
# A discoverable-but-broken install (e.g.
# followthemoney imports fine but a dependency underneath it is broken) must
# degrade gracefully, not raise through entities_available()/diagnostics.
# Runs in a fresh subprocess: sys.modules caching makes this untestable
# in-process once frisket.features.followthemoney has already been imported once.
# --------------------------------------------------------------------------- #


def test_discoverable_but_broken_install_degrades_gracefully(tmp_path: Path) -> None:
    stub_root = tmp_path / "broken_followthemoney_stub"
    stub_pkg = stub_root / "followthemoney"
    stub_pkg.mkdir(parents=True)
    (stub_pkg / "__init__.py").write_text(
        "raise RuntimeError('simulated broken native ICU/normality extension')\n",
        encoding="utf-8",
    )
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(stub_root)!r}); "
        "import frisket.features.followthemoney as ftm; "
        "assert ftm.ENTITIES_AVAILABLE is False, ftm.ENTITIES_AVAILABLE; "
        "ok, err = ftm.entities_available(); "
        "assert ok is False; "
        "from frisket.operability import diagnostics; "
        "report = diagnostics.entities_report(); "
        "assert report['installed'] is False; "
        "full = diagnostics.run_diagnostics(); "
        "assert full['healthy'] is True; "
        "assert full['info']['entities_extra']['installed'] is False; "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout
