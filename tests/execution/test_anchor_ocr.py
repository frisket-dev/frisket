"""End-to-end anchors for routed OCR.

O1-O4 pinned four behaviors for transcription: the short-circuit probe, venue
preservation, refusal honesty, and consent binding at the effect site. This
module asserts the SAME four for a second capability, over the OCR engines
that already existed. What makes them a verdict rather than four more tests is
what they are allowed to depend on: if any of them needed a capability-shaped
branch in the seam, the seam had not generalized. Each assertion below names
the declaration it rides on.

The one stubbed seam per test is the engine call itself (the RapidOCR
subprocess pool; Datalab's HTTP API), taken one level below ``execute`` the way
the end-to-end behavioral guard pin stubs the transcription adapter — so
resolution, the claims gate, the consent record, the attempt mint, the
worker's admission fence, route binding, the credential-use fence, and the
fact/epoch write are all real.

These enter through typed action requests and real attempt authority. The
missing-consent test prepares the same typed program directly so it can remove
the durable consent between preparation and worker admission.
"""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from frisket.engine.runner import MapRunner
from frisket.engine.executor import run_action_spec
from frisket.engine.executor.action_inventory import ExecutorDeps
from frisket.credentials import ResolvedCredential
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore, instance_principal
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.runs import RunResultStore
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.promises import Promise
from frisket.execution.provider import (
    ExecutionCompositionContext,
    open_execution_composition,
)
from tests.execution_composition_helpers import open_attempt_authority

DATALAB_FIXTURES = Path(__file__).parent.parent / "fixtures" / "datalab"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def rapidocr_stub(monkeypatch):
    """Stub ONLY the local RapidOCR page pass (the model inference seam)."""
    calls: list[tuple[str, int]] = []

    async def fake_pages(self, engine, page_paths, ctx, **kwargs):
        calls.append((engine, len(page_paths)))
        return [
            {"text": "hello from the scanner", "blocks": [{"text": "hello"}]}
            for _ in page_paths
        ]

    monkeypatch.setattr(
        "frisket.ops.ocr_engines.OcrEngines.run_engine_on_pages", fake_pages
    )
    # ``execution_scope`` now verifies the optional local model artifact before
    # it reaches this engine seam.  This anchor owns routing and ledger
    # behavior, not the installed model bundle, so keep that preflight outside
    # its scope just as the page pass is.
    monkeypatch.setattr(
        "frisket.ops.ocr_engines_local._rapidocr_model_root_dir",
        lambda language=None: None,
    )
    return calls


class _NullRouter:
    """A router that can hand out an http client and nothing else: the local
    OCR engines need neither, and MapRunner reads ``router.client`` for every
    run."""

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500, json={}))
    )


def _ocr_project(tmp_path: Path) -> tuple[Project, int, int]:
    project = Project.create(tmp_path / "ocr-anchor.frisket", name="ocr")
    sheet_id = project.add_sheet("Scans")
    col = project.add_column(sheet_id, "page", type="image")
    blob = project.add_blob(
        b"\x89PNG\r\n\x1a\n page-bytes",
        filename="scan.png",
        mime="image/png",
        metadata=owned_media_metadata_document(
            probe={"kind": "image", "width": 100, "height": 100}
        ),
    )
    [row_id] = project.add_rows(
        sheet_id,
        [{"page": media_cell(blob, mime="image/png", filename="scan.png")}],
        {"page": col},
    )
    return project, sheet_id, row_id


def _request(sheet_id: int, **params: Any) -> dict[str, Any]:
    return {
        "action_id": "media.ocr",
        "scope": {"kind": "sheet_rows", "sheet_id": sheet_id},
        "params": {"source": "page", "engine": "rapidocr", **params},
        "output_names": {"text": "ocr_text", "blocks": "ocr_text_blocks"},
        "idempotency_key": "ocr-anchor",
    }


def _confirmed_request(project, request, **kwargs):
    deps = kwargs.get("deps") or ExecutorDeps()
    kwargs["deps"] = replace(
        deps,
        consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0")),
    )
    gate = run_action_spec(project, request, project_id="ocr-anchor", **kwargs)
    assert gate.status == "needs_confirmation", gate.errors
    return {**request, "confirmation": gate.errors[0].details["promise_set_hash"]}


def _route_rows(project, run_id: int):
    return project.db.execute(
        "SELECT * FROM routes WHERE subject_kind='run' AND subject_id=? ORDER BY seq",
        (str(run_id),),
    ).fetchall()


# ---------------------------------------------------------------------------
# O1-ocr — the short-circuit probe + the local rent bound
# ---------------------------------------------------------------------------


def test_anchor_ocr_local_run_pays_no_rent(tmp_path, monkeypatch, rapidocr_stub):
    """O1's rent bound, for OCR: the trivial local run pays one config read
    and one recorded route — no consent, no claims, no gate, and no probe of
    any venue but the one it resolved to.

    The declaration doing the work: ``local``'s rapidocr row (transport
    ``local``, egress ``none``, operator ``self``). Nothing here is an OCR
    special case; the free-local short-circuit is the price book answering
    "no SKU" for that (capability, target, funding).
    """
    for name in ("FRISKET_MODELS_URL", "FRISKET_MODELS_TOKEN", "DATALAB_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    project, sheet_id, row_id = _ocr_project(tmp_path)
    try:
        router = _NullRouter()
        composition = open_execution_composition(
            project, router, ExecutionCompositionContext.direct()
        )
        progress = run_action_spec(
            project,
            _request(sheet_id),
            project_id="ocr-anchor",
            router=router,
            deps=ExecutorDeps(execution_composition=composition),
        )
        assert progress.status == "completed", progress.errors
        assert rapidocr_stub == [("rapidocr", 1)]

        # RENT BOUND: exactly the mapped target was ever probed, at every
        # stage (resolution AND the claim-time binding deref).
        probed = {
            target
            for provider in [composition.provider]
            for target in provider.probe_counts
        }
        assert probed == {"local"}, probed
        # At most one probe per phase; typed admission can reuse resolution.
        # The static choice needs no probes, so nothing enumerates venues.
        assert 1 <= composition.provider.probe_counts["local"] <= 2

        run_id = progress.run_id
        store = RouteStore.for_run(project, run_id)
        assert len(_route_rows(project, run_id)) == 1
        route, _head = store.head()
        assert store.consents() == []  # no consent event ever happened
        assert route.engine == "rapidocr"
        assert route.operator == "self"
        assert route.egress_class == "none"
        assert route.credential_source == "local"
        assert route.cost_posture == "operator_borne"
        assert route.target_snapshot["target_id"] == "local"
        assert route.target_snapshot["transport"] == "local"
        # The capability rides the snapshot, which is what lets the run-start
        # fence price this route without asking a recipe what it was.
        assert route.target_snapshot["capability"] == "ocr"

        # Zero user claims — no cost row at all (absence, not a claim of zero).
        [promise_set] = store.promise_sets()
        promises = [Promise.from_row(row) for row in promise_set.promises]
        assert all(p.audience == "system_promise" for p in promises)
        assert not any(p.field == "cost" for p in promises)

        # The text committed, and the fact carries route-derived fields with
        # its epoch linked under the one-ledger invariant enforced for OCR by
        # ``_ROUTED_FACT_CAPABILITIES``.
        columns = {c["name"]: c for c in project.columns(sheet_id)}
        assert (
            project.get_values(
                sheet_id, int(columns["ocr_text"]["id"]), row_ids=[row_id]
            )[row_id]
            == "hello from the scanner"
        )
        [fact] = RunResultStore(project).model_calls(run_id)
        assert fact["capability"] == "ocr"
        assert fact["engine"] == "rapidocr"
        assert fact["provider"] == "local"
        assert fact["provider_kind"] == "local_process"
        assert fact["credential_source"] == "local"
        assert fact["cost_source"] == "free_local"
        assert fact["epoch_id"] is not None
        epoch = project.db.execute(
            "SELECT * FROM binding_epochs WHERE id=?", (fact["epoch_id"],)
        ).fetchone()
        assert epoch is not None and epoch["route_id"] == route.id
        assert store.violations() == []
    finally:
        project.close()


# ---------------------------------------------------------------------------
# O2-ocr — venue preservation + the hosted gate + consent binding
# ---------------------------------------------------------------------------


def _datalab_stub(monkeypatch):
    """The hosted OCR venue, stubbed at its HTTP boundary only."""
    submit = json.loads((DATALAB_FIXTURES / "ocr_submit.json").read_text())
    complete = json.loads((DATALAB_FIXTURES / "ocr_complete.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        return httpx.Response(200, json=complete)

    class StubRouter:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    return StubRouter()


def test_anchor_ocr_hosted_run_compiles_both_claims_gates_and_binds(
    tmp_path, monkeypatch
):
    """A hosted (Datalab) OCR run behaves like a hosted transcription run end
    to end: it compiles BOTH a per-page cost claim and an egress claim, the
    unconfirmed launch is gated on the uncovered one, and the confirmed launch
    records the consent that the worker's own admission fence then verifies
    before any page leaves. (The sibling test below raises the rate so the
    cost claim gates too.)

    The declarations doing the work: the ``datalab`` target row
    (``operator="datalab"``, ``egress_class="third_party_api"``) and the
    per-page SKU. No branch anywhere says "OCR gates" — the egress claim is
    compiled because the venue declares third-party egress, and the cost claim
    because the book has a page rate for it.
    """
    monkeypatch.setenv("DATALAB_API_KEY", "env-key")
    project, sheet_id, row_id = _ocr_project(tmp_path)
    router = _datalab_stub(monkeypatch)
    try:
        spec = _request(sheet_id, engine="datalab")

        # (1) Pin this consent-mechanics anchor to an explicit zero threshold:
        # both cost and egress gate before anything runs. Default under-limit
        # preapproval is covered separately; this test needs the exact-confirm
        # path in order to inspect its durable worker proof below.
        deps = ExecutorDeps(
            consent_coverage=ConsentCoverage(instance_principal(project), Decimal("0"))
        )
        gate = run_action_spec(
            project, spec, router=router, deps=deps, project_id="ocr-anchor"
        )
        assert gate.status == "needs_confirmation", gate.errors
        details = gate.errors[0].details
        fields = {claim["field"] for claim in details["claims"]}
        assert fields == {"cost", "egress_class"}, details
        egress = next(c for c in details["claims"] if c["field"] == "egress_class")
        assert "third-party API" in egress["display"]
        assert details["promise_set_hash"]

        # (2) the confirmed launch runs, and its consent is recorded.
        progress = run_action_spec(
            project,
            {**spec, "confirmation": details["promise_set_hash"]},
            router=router,
            deps=deps,
            project_id="ocr-anchor",
        )
        assert progress.status == "completed", progress.errors
        run_id = progress.run_id
        store = RouteStore.for_run(project, run_id)
        route, head_set = store.head()
        [consent] = store.consents()
        assert consent.promise_set_hash == head_set.promise_set_hash
        assert route.target_snapshot["target_id"] == "datalab"
        assert route.egress_class == "third_party_api"
        assert route.operator == "datalab"

        # The consented set carries a PER-PAGE cost claim: the price book's
        # first non-audio unit, quoted over the pages the run actually
        # selected, bounded and scoreable at the run-start fence.
        promises = [Promise.from_row(row) for row in head_set.promises]
        [cost] = [p for p in promises if p.field == "cost"]
        assert cost.op == "le"
        assert cost.basis["quantity_unit"] == "page"
        assert cost.basis["pricing_key"] == "datalab.ocr.page"
        assert cost.basis["estimated_quantity"] == "1"
        assert cost.audience == "user_claim"

        # (3) the fact is bound to the route, priced, and epoch-linked.
        [fact] = RunResultStore(project).model_calls(run_id)
        assert fact["capability"] == "ocr"
        assert fact["provider"] == "datalab"
        assert fact["provider_kind"] == "platform_api"
        assert fact["epoch_id"] is not None
        assert json.loads(fact["units"])["pages"] == 1
        assert store.violations() == []

        # (4) "what did it cost" answers for OCR through the SAME settlement
        # join: the attempt's pinned basis rated against the pages its own
        # model calls metered. One page at the card-pinned per-page rate.
        from frisket.execution.attempt import run_attempt_receipts

        [receipt] = run_attempt_receipts(project, run_id)
        settlement = receipt["settlement"]
        assert settlement["pricing_key"] == "datalab.ocr.page"
        assert settlement["metered_unit"] == "pages"
        assert settlement["metered_quantity"] == "1"
        assert settlement["rated_calls"] == 1
        assert settlement["charge_usd"] == "0.01"
    finally:
        project.close()


def test_anchor_ocr_accepted_job_is_durable_when_cancelled_before_poll(
    tmp_path, monkeypatch
):
    """Provider acceptance outranks the later cancellation fence.

    The POST returns a real Datalab job id and flips the run's cooperative
    cancel signal before the client can issue its first poll.  There is no
    completed cell and no trustworthy charge amount, but the accepted request
    is already an immutable, attempt-owned, route-bound ledger fact.
    """
    monkeypatch.setenv("DATALAB_API_KEY", "env-key")
    project, sheet_id, row_id = _ocr_project(tmp_path)
    submit = json.loads((DATALAB_FIXTURES / "ocr_submit.json").read_text())
    cancelled = False
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal cancelled
        requests.append(request.method)
        assert request.method == "POST", "cancellation must win before polling"
        cancelled = True
        return httpx.Response(200, json=submit)

    class StubRouter:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    try:
        router = StubRouter()
        deps = ExecutorDeps(
            map_runner_factory=lambda p, r: MapRunner(
                p,
                r,
                authority=open_attempt_authority(p),
                should_cancel=lambda _run_id: cancelled,
            )
        )
        request = _confirmed_request(
            project, _request(sheet_id, engine="datalab"), router=router, deps=deps
        )
        progress = run_action_spec(
            project, request, router=router, deps=deps, project_id="ocr-anchor"
        )
        assert progress.status == "failed", progress.errors
        run = project.db.execute(
            "SELECT status FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        assert run is not None and run["status"] == "cancelled"
        assert requests == ["POST"]
        assert (
            project.db.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=?", (progress.run_id,)
            ).fetchone()[0]
            == 0
        )

        [fact] = RunResultStore(project).model_calls(progress.run_id)
        assert fact["row_id"] == row_id
        assert fact["provider"] == "datalab"
        assert fact["capability"] == "ocr"
        assert fact["request_id"] == submit["request_id"]
        assert fact["provider_cost_usd"] is None
        assert fact["cost_source"] == "unknown"
        assert json.loads(fact["units"])["requests"] == 1
        assert fact["epoch_id"] is not None
        assert fact["attempt_id"] is not None
        assert RouteStore.for_run(project, progress.run_id).violations() == []
    finally:
        project.close()


def test_anchor_ocr_expensive_hosted_run_gates_on_the_cost_claim_too(
    tmp_path, monkeypatch
):
    """The cost half of the hosted gate, shown at a price the standing sub-$1
    threshold cannot cover: the operator's own per-page rate is raised, and the
    gate now carries BOTH claims with the dollar bound rendered from the
    per-page basis.

    Nothing about this is OCR-specific except the unit — it is the same
    threshold, the same coverage rule, and the same claim renderer a hosted
    transcription run meets.
    """
    monkeypatch.setenv("DATALAB_API_KEY", "env-key")
    monkeypatch.setenv("FRISKET_DATALAB_OCR_USD_PER_PAGE", "2.50")
    project, sheet_id, _row_id = _ocr_project(tmp_path)
    router = _datalab_stub(monkeypatch)
    try:
        gate = run_action_spec(
            project,
            _request(sheet_id, engine="datalab"),
            router=router,
            project_id="ocr-anchor",
        )
        assert gate.status == "needs_confirmation", gate.errors
        claims = {
            claim["field"]: claim["display"]
            for claim in gate.errors[0].details["claims"]
        }
        assert set(claims) == {"cost", "egress_class"}
        assert "$2.5" in claims["cost"]
        assert "billed to your own account" in claims["cost"]
    finally:
        project.close()


def test_anchor_ocr_credential_use_fence_refuses_before_the_call(tmp_path, monkeypatch):
    """the credential-use fence's "runs under my key", for OCR: the adapter compares the credential
    CLASS it selected against the class the consent named, BEFORE the first
    page leaves the machine. Force a mismatch (the dispatch selects Frisket's
    platform key on a run consented as operator-borne) and the run halts
    resumably instead of spending.

    The fence is the shared ``credential_use`` constraint; all OCR had to add
    was the call site and the halt vocabulary — the same two lines
    transcription has.
    """
    monkeypatch.setenv("DATALAB_API_KEY", "env-key")
    project, sheet_id, _row_id = _ocr_project(tmp_path)
    router = _datalab_stub(monkeypatch)
    # Stubbed at the SELECTOR, which is the one seam that decides both the key
    # and its provenance: patching the provenance reader alone would leave the
    # dispatch reading a different answer than the fence, which is the class of
    # divergence this fence exists to catch.
    monkeypatch.setattr(
        "frisket.ops.ocr_engines_hosted._datalab_credential",
        lambda ctx: ResolvedCredential("a-platform-key", "platform_key"),
    )
    try:
        request = _confirmed_request(
            project, _request(sheet_id, engine="datalab"), router=router
        )
        progress = run_action_spec(
            project, request, router=router, project_id="ocr-anchor"
        )
        # The invocation halted: nothing was written for the row, and the run
        # is terminalized as resumably cancelled with the halt marker.
        run = project.db.execute(
            "SELECT status, params FROM runs WHERE id=?", (progress.run_id,)
        ).fetchone()
        assert run["status"] == "cancelled"
        assert "promise_violation" in run["params"]
        assert "platform_key" in run["params"]
    finally:
        project.close()


def test_anchor_ocr_org_byok_consent_refuses_the_deployments_env_key(
    tmp_path, monkeypatch
):
    """The BYOK consent, made checkable (the credential-use fence critical 3).

    The scenario, exactly: an org consents under ``cost_posture='org_key'`` —
    the dialog says "billed to your organization's provider key" — and the
    DEPLOYMENT's process environment also holds a ``DATALAB_API_KEY``. The
    selector honestly reports ``local`` because that env key is what resolves;
    while ``local``/``project_key``/``org_byok`` all collapsed into one
    ``operator_key`` class the fence compared operator_key to operator_key,
    passed, and every page was sent and billed on the operator's shared key.

    NOTHING is stubbed here except the HTTP boundary: the selector, the fence
    and the halt are all real, and the assertion that no request was issued is
    what makes "refuses PRE-EFFECT" a fact rather than a claim.
    """
    monkeypatch.setenv("DATALAB_API_KEY", "operator-shared-env-key")
    project, sheet_id, _row_id = _ocr_project(tmp_path)
    requests: list[str] = []
    submit = json.loads((DATALAB_FIXTURES / "ocr_submit.json").read_text())
    complete = json.loads((DATALAB_FIXTURES / "ocr_complete.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.method == "POST":
            return httpx.Response(200, json=submit)
        return httpx.Response(200, json=complete)

    class StubRouter:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # The hosted org composition, resolved for real: the resolver pins
    # credential_source='org_byok' and cost_posture='org_key' from ONE funding
    # fact, so the route row this run is consented under is the row a hosted
    # BYOK deployment would actually write.
    from frisket.execution.credential_use import CredentialOwner, CredentialUseContext
    from frisket.execution.price_book import ByokZero
    from frisket.execution.provider import CompositionFacts, ExecutionComposition

    try:
        spec = _request(sheet_id, engine="datalab")
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
        deps = ExecutorDeps(execution_composition=composition)
        request = _confirmed_request(project, spec, router=router, deps=deps)
        progress = run_action_spec(
            project, request, router=router, deps=deps, project_id="ocr-anchor"
        )
        # The CONSENTED head (seq 1). A dispatch that got past the fence would
        # append a successor recording the credential it observed instead.
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
        # PRE-EFFECT: not one page left the machine, so nothing was billed.
        assert requests == []
    finally:
        project.close()


def test_anchor_ocr_consent_binds_at_the_worker(tmp_path, monkeypatch):
    """Consent binding, the O1/O2 way: delete the run's persisted route
    artifacts and the effect-site fence refuses ``consent_missing`` — the
    admission is not a formality the recipe could skip.

    ``admit_routed`` needed no change for OCR: it reads a route row and a
    consent row, and neither knows what capability wrote it.
    """
    from frisket.execution.runtime_binding import ExecutionRouteVerificationFailed

    monkeypatch.setenv("DATALAB_API_KEY", "env-key")
    project, sheet_id, _row_id = _ocr_project(tmp_path)
    router = _datalab_stub(monkeypatch)
    try:
        from frisket.actions.system import typed_action_for_request
        from frisket.engine.executor.map_rows_action import build_typed_map_rows_plan

        request = _confirmed_request(
            project, _request(sheet_id, engine="datalab"), router=router
        )
        plan = build_typed_map_rows_plan(project, typed_action_for_request(request))
        runner = MapRunner(
            project,
            router,
            authority=open_attempt_authority(project),
            allow_action_lifecycle_only_recipes=True,
        )
        prepared = runner.prepare_run(
            plan.spec_dict(), confirmed=True, program=plan.program
        )
        run_id = prepared.run_id
        assert RouteStore.for_run(project, run_id).head() is not None
        for table in ("routes", "promise_sets", "consents"):
            project.db.execute(
                f"DELETE FROM {table} WHERE subject_kind='run' AND subject_id=?",
                (str(run_id),),
            )
        project.db.commit()
        with pytest.raises(ExecutionRouteVerificationFailed) as refusal:
            open_attempt_authority(project).mint(
                recipe=plan.program,
                spec=plan.spec_dict(),
                run_id=run_id,
                scope=(1,),
            )
        assert refusal.value.code == "consent_missing"
    finally:
        project.close()


# ---------------------------------------------------------------------------
# O3-ocr — refusal honesty through the ONE ability checker
# ---------------------------------------------------------------------------


def test_anchor_ocr_unsupported_option_refuses_honestly(tmp_path, monkeypatch):
    """An authored ``language`` against the gateway's /ocr wire — which
    carries no language field — REFUSES, naming the inability, instead of
    being silently dropped.

    This extends the target-resolution rule to a second capability through a
    declaration (``OcrOptionSupport.language = False`` on the two gateway
    rows) rather than a rule: ``support_inability`` dispatches on the row's own
    capability and asks the OCR checker.
    """
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://gateway.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "tok")
    project, sheet_id, _row_id = _ocr_project(tmp_path)
    try:
        refusal = run_action_spec(
            project,
            _request(sheet_id, engine="dots.mocr", language="japan"),
            router=_NullRouter(),
            project_id="ocr-anchor",
        )
        assert refusal.status == "failed"
        remedy = refusal.errors[0].message
        assert "recognition-language hint" in remedy
        assert "dots.mocr" in remedy
        # And nothing ran: an option no declaring target can honor never
        # reaches a page.
        assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    finally:
        project.close()


def test_anchor_ocr_language_is_served_where_a_target_declares_it(
    tmp_path, monkeypatch, rapidocr_stub
):
    """The other half of refusal honesty: the SAME authored option on a target
    that declares it resolves normally. Without this, "refuses honestly" could
    be satisfied by refusing everything."""
    project, sheet_id, _row_id = _ocr_project(tmp_path)
    try:
        progress = run_action_spec(
            project,
            _request(sheet_id, engine="rapidocr", language="japan"),
            router=_NullRouter(),
            project_id="ocr-anchor",
        )
        assert progress.status == "completed", progress.errors
        route, _head = RouteStore.for_run(project, progress.run_id).head()
        assert route.target_snapshot["target_id"] == "local"
        # The authored option is recorded on the route as authored.
        assert route.options == {"language": "japan", "searchable_pdf": False}
    finally:
        project.close()


# ---------------------------------------------------------------------------
# O4-ocr — venue preservation: one engine, the venue its declaration names
# ---------------------------------------------------------------------------


def test_anchor_ocr_venue_is_declaration_driven_not_liveness_driven(monkeypatch):
    """The deterministic OCR venue rule: the venue a request resolves to comes from the
    declaration order, never from what happens to be live. A gateway-only
    engine with the gateway DOWN refuses ``no_live_target`` with its remedy —
    it does not fall back to a local engine, because a venue flip is a claims
    change.
    """
    from frisket.execution.provider import CompositionFacts
    from frisket.execution.resolver import (
        Refusal,
        Resolution,
        ResolutionRequest,
        resolve,
    )

    for name in ("FRISKET_MODELS_URL", "FRISKET_MODELS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    provider = StaticExecutionTargetProvider()
    refusal = resolve(
        ResolutionRequest(engine="dots.mocr", capability="ocr"),
        provider,
        CompositionFacts(),
    )
    assert isinstance(refusal, Refusal)
    assert refusal.family == "no_live_target"
    assert "FRISKET_MODELS_URL" in refusal.remedy
    # The dead venue was the ONLY one probed (the O1 short-circuit): the
    # static choice needs no probes at all.
    assert provider.probe_counts == {"models-gateway": 1}

    # ...and with the gateway live it resolves THERE, not to the local box.
    monkeypatch.setenv("FRISKET_MODELS_URL", "http://gateway.test")
    monkeypatch.setenv("FRISKET_MODELS_TOKEN", "tok")
    live = resolve(
        ResolutionRequest(engine="dots.mocr", capability="ocr"),
        StaticExecutionTargetProvider(),
        CompositionFacts(),
    )
    assert isinstance(live, Resolution)
    assert live.target.id == "models-gateway"
    assert live.support.transport == "sidecar.ocr"
    assert live.facts.egress_class == "operator_lan"


def test_anchor_ocr_provider_wildcard_row_is_the_ocr_one(monkeypatch):
    """The sharpest same-name case: a ``remote-api:{provider}`` target carries
    TWO ``{provider}/*`` rows, one per capability, and they declare different
    things. An OCR request must select the OCR row — otherwise a VLM OCR run
    would ride a transcription support row and be priced per AUDIO SECOND.

    Cut the capability filter out of the resolver's wildcard lookup and this
    test goes red on the transport-neutral half (the row's own capability)
    AND on the refusal below.
    """
    from frisket.execution.provider import CompositionFacts
    from frisket.execution.resolver import (
        Refusal,
        Resolution,
        ResolutionRequest,
        resolve,
    )

    monkeypatch.setenv("GEMINI_API_KEY", "key")
    resolved = resolve(
        ResolutionRequest(engine="gemini/gemini-3.5-flash", capability="ocr"),
        StaticExecutionTargetProvider(),
        CompositionFacts(),
    )
    assert isinstance(resolved, Resolution)
    assert resolved.target.id == "remote-api:gemini"
    assert resolved.support.capability == "ocr"
    # ...and the OCR row's honest declaration refuses searchable_pdf, which a
    # VLM cannot compose (it returns no geometry). The transcription row on
    # the same target has no opinion about searchable_pdf at all.
    refusal = resolve(
        ResolutionRequest(
            engine="gemini/gemini-3.5-flash",
            capability="ocr",
            options={"searchable_pdf": True},
        ),
        StaticExecutionTargetProvider(),
        CompositionFacts(),
    )
    assert isinstance(refusal, Refusal)
    assert refusal.family == "no_capable_target"
    assert "searchable PDF" in refusal.remedy


def test_anchor_ocr_one_venue_two_capabilities_keeps_them_apart(monkeypatch):
    """The generalization stated as a fact about the target rows: the SAME
    venue serves both capabilities, and asking it about one never returns the
    other's engine row. ``datalab`` is the sharp case — the id names a hosted
    OCR engine here and a hosted document-conversion engine in the
    to_markdown roster."""
    from frisket.execution.definitions import build_static_targets

    local = next(t for t in build_static_targets() if t.id == "local")
    by_capability = {
        (row.capability, row.engine): row.transport for row in local.engines
    }
    assert ("transcribe", "faster_whisper") in by_capability
    assert ("ocr", "rapidocr") in by_capability
    # ...and no row is reachable from the other capability's lookup.
    from frisket.execution.resolver import preferred_static_choice

    targets = build_static_targets()
    assert (
        preferred_static_choice("rapidocr", {}, targets, capability="transcribe")
        is None
    )
    choice = preferred_static_choice("rapidocr", {}, targets, capability="ocr")
    assert choice is not None and choice[0].id == "local"


def test_anchor_ocr_every_roster_engine_has_exactly_one_venue():
    """The arming fence for OCR dispatch (routed OCR's own caveat, made loud).

    Current OCR engine transport and run-scoped resource declarations have one
    venue each. A future second venue must reconcile those declarations with
    the adapter's admitted transport check, rather than silently changing where
    an existing engine executes.
    """
    from frisket.contracts.actions.schemas._engines import OCR_ENGINE_TABLE
    from frisket.execution.definitions import build_static_targets

    venues: dict[str, list[str]] = {}
    for target in build_static_targets():
        for row in target.engines:
            if row.capability == "ocr":
                venues.setdefault(row.engine, []).append(target.id)
    multi = {
        entry.id: venues.get(entry.id, [])
        for entry in OCR_ENGINE_TABLE
        if len(venues.get(entry.id, [])) != 1
    }
    assert multi == {}, (
        f"A second venue for {sorted(multi)} requires explicit reconciliation "
        "with OCR transport and run-scoped resource declarations."
    )
