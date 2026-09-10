from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server import provider_config
from frisket.server.app import create_app

ROOT = Path(__file__).resolve().parents[2]


def test_rapidocr_capacity_is_diagnose_info_not_application_health(
    tmp_path, monkeypatch
):
    """Optional-engine tuning belongs to operator diagnostics, not liveness."""

    pytest.importorskip("rapidocr")
    from frisket.operability import diagnostics

    runtime = {
        "basis": "multi_row_default",
        "config_mode": "automatic",
        "overrides": [],
        "resolved": True,
        "effective_cpus": 4,
        "effective_memory_bytes": 8 * 1024**3,
        "workers": 2,
        "onnx_intra_threads": 2,
        "onnx_inter_threads": 1,
        "opencv_requested_threads": 1,
        "memory_limit_mb": 3584,
    }
    monkeypatch.setattr(diagnostics, "_rapidocr_runtime_report", lambda: runtime)
    client = TestClient(create_app(tmp_path / "ws"))

    diagnosed = client.get("/api/diagnose")
    assert diagnosed.status_code == 200
    assert diagnosed.json()["info"]["local_engines"]["rapidocr_runtime"] == runtime

    health = client.get("/api/health")
    assert health.status_code == 200
    assert "local_engines" not in health.json()
    assert "rapidocr_runtime" not in health.json()


# ---------------------------------------------------------------------------
# Local tier: an explicitly injected router


def test_diagnose_reports_explicitly_injected_router_keys(tmp_path, monkeypatch):
    """create_app(router=ModelRouter(keys={"anthropic": ...})) must show up
    as configured; the former behavior returned ``configured=[]``."""
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    router = ModelRouter(keys={"anthropic": "sk-test-injected"})
    client = TestClient(create_app(tmp_path / "ws", router=router))
    resp = client.get("/api/diagnose")
    assert resp.status_code == 200
    configured = resp.json()["info"]["model_providers"]["configured"]
    assert configured == ["anthropic"]


# ---------------------------------------------------------------------------
# Local tier: no explicit router, but a UI-saved workspace-file key
# (.frisket/provider_keys.json via provider_config.save_local_provider_key)


def test_diagnose_reports_local_workspace_file_key_with_no_injected_router(
    tmp_path, monkeypatch
):
    """The plain local-server path (no router injected at all, as the real
    CLI-launched server runs) must still surface a key the user saved
    through the Configure AI providers UI -- Workspace.router_for's file
    layer, which the pre-fix bare ModelRouter() never consulted."""
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    provider_config.save_local_provider_key(ws_root, "openai", "sk-file-saved")
    client = TestClient(create_app(ws_root))
    resp = client.get("/api/diagnose")
    assert resp.status_code == 200
    configured = resp.json()["info"]["model_providers"]["configured"]
    assert configured == ["openai"]


def test_diagnose_local_tier_env_still_wins_over_file_key(tmp_path, monkeypatch):
    """resolve_effective_keys' documented precedence (env wins on conflict)
    must survive through the diagnose route too -- not just the file layer,
    the whole precedence chain."""
    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    provider_config.save_local_provider_key(ws_root, "anthropic", "sk-file-saved")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env-wins")
    for var in ("OPENAI_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    client = TestClient(create_app(ws_root))
    resp = client.get("/api/diagnose")
    configured = resp.json()["info"]["model_providers"]["configured"]
    assert configured == ["anthropic"]


# ---------------------------------------------------------------------------
# Hosted tier: per-org isolation -- an org's own BYO key shows up, an
# operator-seeded platform default is truthfully reported (not a leak, see
# module docstring), and org A's key is never visible to org B (the real
# forbidden outcome).


def _validated_org_key_body(provider: str, key: str) -> dict[str, str]:
    return {
        "provider": provider,
        "key": key,
        "validation_token": provider_config.issue_validation_token(provider, key),
    }
