"""Consent coverage remains scoped to the authenticated principal."""

from decimal import Decimal

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.engine.executor import ExecutorDeps
from frisket.engine.store import Project
from frisket.engine.store.execution_routes import RouteStore
from frisket.execution.consent_coverage import ConsentCoverage
from frisket.execution.promises import Promise, PromiseSet
from frisket.execution.resolve_for_action import (
    action_identity_hash,
    consented_set_hash,
    exact_match_consent_covers,
    project_uncovered_user_claims,
)
from frisket.server.app import create_app
from http_test_helpers import drain_queue
from tests.execution.test_billed_cost_consent_seam import (
    _StubAdapter,
    _classify_action,
)
from tests.execution.typed_transcription_helpers import transcription_spec


def _paid_claims() -> PromiseSet:
    return PromiseSet.make(
        (
            Promise.make("cost", "le", "1.50", audience="user_claim"),
            Promise.make(
                "egress_class",
                "eq",
                "third_party_api",
                audience="user_claim",
            ),
        )
    )


def test_same_project_principals_cannot_borrow_preapproval_or_exact_consent(
    tmp_path,
) -> None:
    project = Project.create(tmp_path / "shared.frisket")
    spec = transcription_spec()
    promises = _paid_claims()
    alice = ConsentCoverage("hosted-user:alice", Decimal("2.00"))
    bob = ConsentCoverage("hosted-user:bob", Decimal("0.50"))

    assert (
        project_uncovered_user_claims(
            project, promises, spec=spec, consent_coverage=alice
        )
        == []
    )
    assert project_uncovered_user_claims(
        project, promises, spec=spec, consent_coverage=bob
    ) == list(promises.promises)

    RouteStore.for_run(project, 7).record_consent(
        action_identity_hash=action_identity_hash(spec),
        promise_set_hash=consented_set_hash(promises),
        actor=alice.principal,
    )

    assert exact_match_consent_covers(project, spec, promises, consent_coverage=alice)
    assert not exact_match_consent_covers(project, spec, promises, consent_coverage=bob)
    assert project_uncovered_user_claims(
        project, promises, spec=spec, consent_coverage=bob
    ) == list(promises.promises)


def test_requestless_worker_uses_the_principal_pinned_at_admission(
    tmp_path, monkeypatch
) -> None:
    alice = None
    wrong_worker_actor = None
    factory_requests = []

    def deps_factory(_project_id, request):
        factory_requests.append(request)
        assert alice is not None and wrong_worker_actor is not None
        coverage = alice if request is not None else wrong_worker_actor
        return ExecutorDeps(consent_coverage=coverage)

    adapter = _StubAdapter()
    router = ModelRouter(keys={"anthropic": "test"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter  # noqa: SLF001
    client = TestClient(
        create_app(
            tmp_path / "workspace",
            router=router,
            executor_deps_factory=deps_factory,
        )
    )
    project_id = client.post("/api/projects", json={"name": "Actor pin"}).json()["id"]
    project = client.app.state.workspace.get(project_id)
    from frisket.engine.store.execution_routes import instance_principal

    installation = instance_principal(project)
    alice = ConsentCoverage(f"{installation}:user:alice", Decimal("2.00"))
    wrong_worker_actor = ConsentCoverage(f"{installation}:user:bob", Decimal("0"))
    sheet_id = project.add_sheet("data")
    column_id = project.add_column(sheet_id, "text", type="text")
    project.add_rows(sheet_id, [{"text": "A" * 800}], {"text": column_id})

    accepted = client.post(
        f"/api/projects/{project_id}/actions/v1/run",
        json=_classify_action(sheet_id),
    )
    assert accepted.status_code == 200, accepted.text
    run_id = int(accepted.json()["run_id"])
    assert (
        project.db.execute(
            "SELECT consent_principal FROM runs WHERE id=?", (run_id,)
        ).fetchone()["consent_principal"]
        == alice.principal
    )
    consent = project.db.execute(
        "SELECT actor, grant_basis FROM consents "
        "WHERE subject_kind='run' AND subject_id=?",
        (str(run_id),),
    ).fetchone()
    assert dict(consent) == {
        "actor": alice.principal,
        "grant_basis": "cost_preapproved",
    }

    drain_queue(client, worker_id="principal-recovery")

    assert any(request is None for request in factory_requests)
    assert len(adapter.calls) == 1
    assert (
        project.db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()[
            "status"
        ]
        == "completed"
    )
