"""`frisket doctor embeddings` — deterministic embeddings live-proof harness.

The doctor reports rather than crashing when native embedding dependencies are missing:
config is INFO, not a failure. Every assertion here is deterministic (env dicts
+ a keyless/fake router + a tmp workspace) — NO network, NO real keys. The
no-leak assertion is the load-bearing one: a present provider key must surface
as ``key_present: True`` BY NAME ONLY, and no secret substring may appear
anywhere in the rendered report.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from frisket.operability.doctor.embeddings import report_embeddings

# The venv console script for [project.scripts] frisket, invoked directly
# rather than through `uv run` so env-manager chatter cannot pollute the
# CLI's output (the --json test parses stdout).
FRISKET_CLI = str(Path(sys.executable).with_name("frisket"))


class _FakeRouter:
    """Mirrors ModelRouter().providers() — adapter NAMES only, never values."""

    def __init__(self, names: list[str]):
        self._names = list(names)

    def providers(self) -> list[str]:
        return list(self._names)


def test_keyless_env_all_live_proofs_would_skip(tmp_path):
    report = report_embeddings(env={}, router=_FakeRouter([]), workspace_root=tmp_path)
    assert isinstance(report, dict)
    # providers section: every provider reported, all key_present False
    provs = report["providers"]
    for name in ("openai", "gemini", "openrouter", "anthropic"):
        assert provs[name]["key_present"] is False
    # live_proofs: every keyless REMOTE proof would_skip with a reason. (The
    # local-real proof mirrors fastembed availability, which is environment-
    # dependent — test_disable_local_embed pins its skip deterministically.)
    proofs = report["live_proofs"]
    assert proofs, "live_proofs must enumerate the optional/key-gated checks"
    for name, proof in proofs.items():
        assert proof.get("reason"), f"{name} needs a reason"
    for name in ("remote_openai", "remote_gemini", "remote_openrouter"):
        assert proofs[name]["status"] == "would_skip", (name, proofs[name])


def test_present_key_reports_present_without_leaking_value(tmp_path):
    secret = "sk-SECRETVALUE"
    report = report_embeddings(
        env={"OPENAI_API_KEY": secret},
        router=_FakeRouter(["openai"]),
        workspace_root=tmp_path,
    )
    assert report["providers"]["openai"]["key_present"] is True
    # the openai remote live-proof would now run (key present)
    assert report["live_proofs"]["remote_openai"]["status"] == "would_run"
    # NO-LEAK: the secret value must not appear anywhere in the report.
    blob = json.dumps(report)
    assert "SECRETVALUE" not in blob
    assert secret not in blob


def test_disable_local_embed_reports_disabled_and_skips_local_proof(tmp_path):
    report = report_embeddings(
        env={"FRISKET_DISABLE_LOCAL_EMBED": "1"},
        router=_FakeRouter([]),
        workspace_root=tmp_path,
    )
    fe = report["fastembed"]
    assert fe["disabled_by_env"] is True
    local = report["live_proofs"]["local_real"]
    assert local["status"] == "would_skip"
    assert local.get("reason")


def test_sidecar_unconfigured_when_url_unset(tmp_path):
    report = report_embeddings(env={}, router=_FakeRouter([]), workspace_root=tmp_path)
    sidecar = report["sidecar"]
    assert sidecar["configured"] is False
    assert sidecar["status"] == "unconfigured"
    # no network probe happened: no reachable/engines keys forced on us
    assert "reachable" not in sidecar


def test_cli_doctor_embeddings_exits_zero():
    proc = subprocess.run(
        [FRISKET_CLI, "doctor", "embeddings"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**_clean_env(), "FRISKET_DISABLE_LOCAL_EMBED": "1"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "embeddings" in proc.stdout.lower()


def test_cli_doctor_embeddings_json_flag_exits_zero():
    proc = subprocess.run(
        [FRISKET_CLI, "doctor", "embeddings", "--json"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**_clean_env(), "FRISKET_DISABLE_LOCAL_EMBED": "1"},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert "providers" in payload and "live_proofs" in payload


def _clean_env() -> dict[str, str]:
    """A subprocess env with no real provider keys / sidecar URL so the CLI run
    is deterministic regardless of the developer's shell."""
    import os

    drop = {
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTHROPIC_API_KEY",
        "FRISKET_MODELS_URL",
        "FRISKET_MODELS_TOKEN",
    }
    return {k: v for k, v in os.environ.items() if k not in drop}


def test_sidecar_url_credentials_are_redacted(tmp_path, monkeypatch):
    # Review (MED): FRISKET_MODELS_URL may carry user:pass@ — the doctor must NOT echo
    # the secret in the url field OR in the (URL-bearing) probe error detail. Monkeypatch
    # the probe to fail with the full URL embedded, mirroring a real httpx error.
    import httpx

    def boom(url, **kwargs):
        raise httpx.ConnectError(f"cannot connect to {url}")

    monkeypatch.setattr(httpx, "get", boom)
    report = report_embeddings(
        env={"FRISKET_MODELS_URL": "https://user:SUPERSECRET@models.internal"},
        router=_FakeRouter([]),
        workspace_root=tmp_path,
    )
    blob = json.dumps(report)
    assert "SUPERSECRET" not in blob  # no-leak: not in url, not in detail
    sidecar = report["sidecar"]
    assert sidecar["configured"] is True
    assert "SUPERSECRET" not in sidecar["url"] and "user:" not in sidecar["url"]
    assert sidecar["url"] == "https://models.internal"
