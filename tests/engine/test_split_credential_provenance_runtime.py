from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from runner_test_helpers import run_action_with_exact_confirmation

pytestmark = pytest.mark.gap

_VERSION_FIELD_NAMES = ("fact_version", "accounting_version")
_SECRETISH_FIELD = re.compile(
    r"(?i)(?:api[_-]?key|secret|token|password|credential[_-]?value|key[_-]?hint)"
)
_VERSION_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]*v[1-9][0-9]*$")


class _StubAdapter:
    """A controlled remote response; it receives no key and makes no network call."""

    async def complete(self, req: Any, client: Any):  # noqa: ANN001
        from frisket.ai.llm import LLMResponse

        reply = {
            "beat": "accountability",
            "beat_justification": "The contract was awarded without bidding.",
            "beat_confidence": 0.93,
        }
        return LLMResponse(
            content=json.dumps(reply),
            data=reply,
            tokens_in=11,
            tokens_out=7,
            cost=0.004,
            model=req.model,
        )


def _project(tmp_path: Path, name: str):
    from frisket.engine.store import Project

    project = Project.create(tmp_path / f"{name}.frisket", name=name)
    sheet_id = project.add_sheet("Stories")
    columns = {"story": project.add_column(sheet_id, "story", type="text")}
    project.add_rows(
        sheet_id,
        [{"story": "The city awarded a no-bid contract."}],
        columns,
    )
    return project, sheet_id


def _map_action(sheet_id: int, *, key: str) -> dict[str, Any]:
    return {
        "action_id": "map.classify",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {
            "source": ["story"],
            "engine": "llm",
            "model": "openai/gpt-5-mini",
            "context": "Classify a city-news story.",
            "fields": [
                {
                    "name": "beat",
                    "type": "category",
                    "labels": ["accountability", "other"],
                    "description": "Editorial beat.",
                }
            ],
            "include_justification": True,
            "include_confidence": True,
        },
        "idempotency_key": key,
    }


def _persisted_remote_fact(project: Any, sheet_id: int, router: Any, *, key: str):
    """Run a real map action and return its durable model-call rows."""
    from frisket.engine.store.runs import RunResultStore

    # The adapter replacement is deliberately below the router composition
    # seam: the composed router still selects the real configured credential,
    # while the request itself is hermetic. Preserve an injected stub so the
    # no-overlay case also proves composition retained the adapter instance.
    if not isinstance(router._adapters.get("openai"), _StubAdapter):  # noqa: SLF001
        router._adapters["openai"] = _StubAdapter()  # noqa: SLF001
    result = run_action_with_exact_confirmation(
        project,
        _map_action(sheet_id, key=key),
        project_id="credential-provenance-contract",
        router=router,
    )
    assert result.status == "completed", result.errors
    assert result.run_id is not None
    rows = RunResultStore(project).model_calls(result.run_id)
    assert rows, "the real map runner did not persist a remote model-call fact"
    return rows


def _assert_durable_fact_contract(
    rows: list[Any], *, expected_source: str, forbidden_values: set[str]
) -> None:
    for row in rows:
        fields = set(row.keys())
        assert row["credential_source"] == expected_source, (
            "persisted fact provenance must describe the credential that won "
            f"composition; expected {expected_source!r}, got "
            f"{row['credential_source']!r}"
        )
        assert row["credential_source"] != "configured_key", (
            "configured_key is ambiguous runtime provenance, not a billable "
            "credential source"
        )

        version_fields = [name for name in _VERSION_FIELD_NAMES if name in fields]
        assert len(version_fields) == 1, (
            "each persisted model-call fact needs exactly one explicit "
            "fact/accounting version field (fact_version or accounting_version)"
        )
        version = row[version_fields[0]]
        assert isinstance(version, str) and _VERSION_TOKEN.fullmatch(version), (
            "persisted model-call facts need a supported, named version token "
            f"(got {version!r}) so private settlement can reject version skew"
        )

        serialized = json.dumps(dict(row), sort_keys=True, default=str)
        for field in fields:
            assert not _SECRETISH_FIELD.search(field), (
                f"durable model-call fact field {field!r} suggests credential material"
            )
        for value in forbidden_values:
            assert value not in serialized, (
                "a durable model-call fact retained credential material or a "
                "credential hint rather than only its source enum"
            )


def _assert_selected_key(router: Any, expected: str) -> None:
    """Verify composition selected the expected test credential without logging it."""
    assert router.configured_keys().get("openai") == expected, (
        "credential precedence selected the wrong layer before the remote call"
    )


def test_sync_workspace_keeps_project_org_and_local_provenance_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspace must preserve the source after synchronous key composition."""
    from frisket.ai.llm import ModelRouter
    from frisket.server.provider_config import save_local_provider_key
    from frisket.server.workspace import Workspace

    platform = "test-platform-credential"
    org = "test-org-byok-credential"
    project_key = "test-project-credential"
    local = "test-local-operator-credential"

    # The injected hosted router declares its ownership explicitly; router
    # injection alone cannot distinguish org BYOK from platform or local keys.
    # A project key must still win it.
    project, sheet_id = _project(tmp_path, "sync-project")
    hosted_router = ModelRouter(
        keys={"openai": org},
        key_sources={"openai": "org_byok"},
    )
    hosted_adapter = _StubAdapter()
    hosted_router._adapters["openai"] = hosted_adapter  # noqa: SLF001
    hosted = Workspace(tmp_path / "hosted", router=hosted_router)
    monkeypatch.setattr(project, "provider_model_keys", lambda: {"openai": project_key})
    try:
        router = hosted.router_for(project)
        _assert_selected_key(router, project_key)
        rows = _persisted_remote_fact(project, sheet_id, router, key="sync-project")
        _assert_durable_fact_contract(
            rows,
            expected_source="project_key",
            forbidden_values={platform, org, project_key, local},
        )
    finally:
        project.close()

    # With no project key, the same hosted composition uses the org BYOK key.
    project, sheet_id = _project(tmp_path, "sync-org")
    monkeypatch.setattr(project, "provider_model_keys", lambda: {})
    try:
        router = hosted.router_for(project)
        assert router is hosted_router
        assert router._adapters["openai"] is hosted_adapter  # noqa: SLF001
        _assert_selected_key(router, org)
        rows = _persisted_remote_fact(project, sheet_id, router, key="sync-org")
        _assert_durable_fact_contract(
            rows,
            expected_source="org_byok",
            forbidden_values={platform, org, project_key, local},
        )
    finally:
        project.close()

    # A key belonging to the local/self-host workspace is operator-owned, not
    # an unlabelled configured key and not a managed-platform credential.
    local_root = tmp_path / "local"
    save_local_provider_key(local_root, "openai", local)
    local_workspace = Workspace(local_root)
    project, sheet_id = _project(tmp_path, "sync-local")
    monkeypatch.setattr(project, "provider_model_keys", lambda: {})
    try:
        router = local_workspace.router_for(project)
        _assert_selected_key(router, local)
        rows = _persisted_remote_fact(project, sheet_id, router, key="sync-local")
        _assert_durable_fact_contract(
            rows,
            expected_source="local",
            forbidden_values={platform, org, project_key, local},
        )
    finally:
        project.close()


@pytest.mark.parametrize(
    ("project_keys", "org_keys", "expected_key", "expected_source"),
    [
        (
            {"openai": "test-project-credential"},
            {"openai": "test-org-byok-credential"},
            "test-project-credential",
            "project_key",
        ),
        (
            {},
            {"openai": "test-org-byok-credential"},
            "test-org-byok-credential",
            "org_byok",
        ),
        ({}, {}, "test-platform-credential", "platform_key"),
    ],
    ids=["project-over-org-over-platform", "org-over-platform", "platform-fallback"],
)
def test_queued_router_keeps_runtime_precedence_in_persisted_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    project_keys: dict[str, str],
    org_keys: dict[str, str],
    expected_key: str,
    expected_source: str,
) -> None:
    """The worker's independently composed router has the same durable truth."""
    from frisket.engine.jobs.runs import _router_for
    from frisket.ai.llm import ModelRouter

    platform = "test-platform-credential"
    org = "test-org-byok-credential"
    project_key = "test-project-credential"
    project, sheet_id = _project(tmp_path, f"queued-{expected_source}")
    monkeypatch.setattr(project, "provider_model_keys", lambda: project_keys)
    try:
        base_router = ModelRouter(
            keys={"openai": platform},
            key_sources={"openai": "platform_key"},
        )
        base_adapter = _StubAdapter()
        if not project_keys and not org_keys:
            base_router._adapters["openai"] = base_adapter  # noqa: SLF001
        router = _router_for(
            project,
            base_router,
            keys=org_keys,
        )
        if not project_keys and not org_keys:
            assert router is base_router
            assert router._adapters["openai"] is base_adapter  # noqa: SLF001
        _assert_selected_key(router, expected_key)
        rows = _persisted_remote_fact(
            project,
            sheet_id,
            router,
            key=f"queued-{expected_source}",
        )
        if not project_keys and not org_keys:
            assert router._adapters["openai"] is base_adapter  # noqa: SLF001
        _assert_durable_fact_contract(
            rows,
            expected_source=expected_source,
            forbidden_values={platform, org, project_key},
        )
    finally:
        project.close()
