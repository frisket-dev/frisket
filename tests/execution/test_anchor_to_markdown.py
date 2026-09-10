"""The to_markdown org-BYOK credential-use anchor — the twin of
``tests/execution/test_anchor_ocr.py``'s
``test_anchor_ocr_org_byok_consent_refuses_the_deployments_env_key``, for the
Datalab conversion dispatch.

The scenario, exactly: an org consents under ``cost_posture='org_key'`` — the
dialog says "billed to your organization's provider key" — and the
DEPLOYMENT's process environment also holds a ``DATALAB_API_KEY``. The
selector honestly reports ``local`` because that env key is what resolves;
without the pre-effect credential-USE fence, that mismatch would silently
bill the deployment's shared key against a consent naming the org's own.

NOTHING is stubbed here except the HTTP boundary: the selector, the fence and
the halt are all real, and the assertion that no request was issued is what
makes "refuses PRE-EFFECT" a fact rather than a claim.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from frisket.engine.runner import MapRunner
from frisket.engine.runner.validation import ClaimsGate
from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan
from frisket.actions.system import typed_action_for_request
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import instance_principal
from frisket.engine.store.media_blobs import media_cell
from frisket.execution.attempt_authority import AttemptAuthority
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.definitions import StaticExecutionTargetProvider
from runner_test_helpers import run_with_output_claim

DATALAB_FIXTURES = Path(__file__).parent.parent / "fixtures" / "datalab"


def _to_markdown_project(tmp_path: Path) -> tuple[Project, int, int]:
    project = Project.create(tmp_path / "to-markdown-anchor.frisket", name="convert")
    sheet_id = project.add_sheet("Docs")
    col = project.add_column(sheet_id, "doc", type="file")
    blob = project.add_blob(
        b"%PDF-1.4 two-page-doc",
        filename="d.pdf",
        mime="application/pdf",
        metadata=owned_media_metadata_document(probe={"kind": "document", "pages": 2}),
    )
    [row_id] = project.add_rows(
        sheet_id,
        [
            {
                "doc": media_cell(
                    blob,
                    mime="application/pdf",
                    filename="d.pdf",
                )
            }
        ],
        {"doc": col},
    )
    return project, sheet_id, row_id


def _request(sheet_id: int) -> dict:
    spec = {
        "action_id": "media.to_markdown",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "doc", "engine": "datalab"},
        "output_names": {"markdown": "markdown"},
        "idempotency_key": "markdown-org-key-anchor",
    }
    return spec


def _exactly_confirmed_spec(runner: MapRunner, spec: dict, *, program) -> dict:
    """Echo the exact claim set the fresh launch presented — the same strict
    confirmation socket a production caller enters through (byte-identical
    to test_anchor_ocr.py's helper)."""
    with pytest.raises(ClaimsGate) as gate:
        runner.prepare_run(dict(spec), program=program)
    return dict(spec, consented_promise_set_hash=gate.value.promise_set_hash)


def _route_rows(project, run_id: int):
    return project.db.execute(
        "SELECT * FROM routes WHERE subject_kind='run' AND subject_id=? ORDER BY seq",
        (str(run_id),),
    ).fetchall()


def test_anchor_to_markdown_org_byok_consent_refuses_the_deployments_env_key(
    tmp_path, monkeypatch
):
    """The BYOK consent, made checkable — for a second Datalab-backed
    capability (document.convert), same shape as O2-ocr's the credential-use fence critical 3."""
    monkeypatch.setenv("DATALAB_API_KEY", "operator-shared-env-key")
    project, sheet_id, _row_id = _to_markdown_project(tmp_path)
    requests: list[str] = []
    submit = json.loads((DATALAB_FIXTURES / "convert_submit.json").read_text())
    complete = json.loads((DATALAB_FIXTURES / "convert_complete.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        return httpx.Response(200, json=complete)

    class StubRouter:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # The hosted org composition, resolved for real: the resolver pins
    # credential_source='org_byok' and cost_posture='org_key' from ONE
    # funding fact, so the route row this run is consented under is the row
    # a hosted BYOK deployment would actually write.
    from frisket.execution.credential_use import CredentialOwner, CredentialUseContext
    from frisket.execution.price_book import ByokZero
    from frisket.execution.provider import CompositionFacts, ExecutionComposition

    try:
        plan = build_typed_map_rows_plan(
            project, typed_action_for_request(_request(sheet_id))
        )
        spec = plan.spec_dict()
        router = StubRouter()
        deployment = CredentialOwner.deployment("hosted-deployment")
        organization = CredentialOwner.organization("org-a")
        composition = ExecutionComposition(
            facts=CompositionFacts(
                edition="hosted",
                org_id="org-a",
                funding=ByokZero(),
            ),
            provider=StaticExecutionTargetProvider(secrets=project, router=router),
            credential_use_context=CredentialUseContext(
                cost_posture="org_key",
                consented_owner=organization,
                selected_owner=organization,
                deployment_owner=deployment,
            ),
        )
        runner = MapRunner(
            project,
            router,
            authority=AttemptAuthority(project, composition=composition),
            consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0")),
            execution_composition=composition,
            allow_action_lifecycle_only_recipes=True,
        )
        confirmed_spec = _exactly_confirmed_spec(runner, spec, program=plan.program)
        progress = asyncio.run(
            run_with_output_claim(
                runner,
                confirmed_spec,
                program=plan.program,
                confirmed=True,
            )
        )
        # The CONSENTED head (seq 1). A dispatch that got past the fence
        # would append a successor recording the credential it observed
        # instead.
        routes = _route_rows(project, progress.run_id)
        assert len(routes) == 1, [dict(row) for row in routes]
        assert routes[0]["cost_posture"] == "org_key"
        assert routes[0]["credential_source"] == "org_byok"
        run = project.db.execute(
            "SELECT status, params FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        assert run["status"] == "cancelled"
        assert "promise_violation" in run["params"]
        # The refusal names what was selected and what was consented.
        assert "'platform_key'" in run["params"]
        assert "org_key" in run["params"]
        # PRE-EFFECT: not one request left the machine, so nothing was billed.
        assert requests == []
    finally:
        project.close()
