"""Frozen contract for ``split-2-sensitive-write-path-v1``.

This gate intentionally freezes only the small public seams needed before the
Product telemetry now uses a small closed browser event vocabulary:

* ``POST /api/projects`` accepts canonical bundle metadata ``sensitive``;
* ``PATCH /api/projects/{pid}/sensitivity`` is the sole mutation route and is
  declared owner-only in the neutral endpoint catalog;
* the project-bound telemetry route checks canonical sensitivity before egress;
* ``frisket.corrections.contribution`` owns the separately enabled, injected
  Frisket-directed correction contribution action.

The event schema rejects raw content and arbitrary values.
"""

from __future__ import annotations

import importlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.engine.store import Project
from tests.team_setup_helpers import (
    claim_server,
    seed_member_invite,
    sign_in_with_magic_link,
)


pytestmark = pytest.mark.gap


class _Destination:
    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> None:
        self.attempts.append(payload)


class _CorrectionSender:
    def __init__(self) -> None:
        self.attempts: list[dict[str, Any]] = []

    def send(self, payload: dict[str, Any]) -> None:
        self.attempts.append(payload)


def _correction_api() -> tuple[Any, Any]:
    try:
        corrections = importlib.import_module(
            "frisket.features.corrections.contribution"
        )
        return (
            corrections.CorrectionContributor,
            corrections.CorrectionTrainingExporter,
        )
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(
            f"sensitive automated-egress seam is not implemented: {exc}",
            pytrace=False,
        )


def test_sensitive_is_canonical_create_metadata_and_bundle_round_trips(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    client = TestClient(create_app(workspace))

    made = client.post(
        "/api/projects", json={"name": "Protected sources", "sensitive": True}
    )
    assert made.status_code == 200, made.text
    pid = made.json()["id"]
    assert made.json()["sensitive"] is True
    assert client.get(f"/api/projects/{pid}").json()["sensitive"] is True

    manifest_path = workspace / f"{pid}.frisket" / "manifest.json"
    assert json.loads(manifest_path.read_text())["sensitive"] is True

    exported = client.get(f"/api/projects/{pid}/export")
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as bundle:
        assert json.loads(bundle.read("manifest.json"))["sensitive"] is True
    archive = tmp_path / "protected.frisket.zip"
    archive.write_bytes(exported.content)
    restored = Project.import_bundle(archive, tmp_path / "restored.frisket")
    try:
        assert restored.project_metadata()["sensitive"] is True
    finally:
        restored.close()


def test_only_catalog_declared_sensitivity_route_can_mutate_the_flag(
    tmp_path: Path,
) -> None:
    from frisket.contracts.http.endpoint_catalog import BASE_ENDPOINT_CATALOG

    client = TestClient(create_app(tmp_path / "workspace"))
    created = client.post(
        "/api/projects", json={"name": "Ordinary", "sensitive": False}
    )
    assert created.status_code == 200, created.text
    pid = created.json()["id"]

    generic = client.patch(
        f"/api/projects/{pid}", json={"name": "Still ordinary", "sensitive": True}
    )
    assert generic.status_code == 422, generic.text
    assert client.get(f"/api/projects/{pid}").json()["sensitive"] is False

    declared = [
        entry
        for entry in BASE_ENDPOINT_CATALOG
        if entry.method == "PATCH" and entry.route_name == "update_project_sensitivity"
    ]
    assert len(declared) == 1
    assert declared[0].route_name == "update_project_sensitivity"
    assert declared[0].project_role == "owner"

    explicit = client.patch(
        f"/api/projects/{pid}/sensitivity", json={"sensitive": True}
    )
    assert explicit.status_code == 200, explicit.text
    assert explicit.json()["sensitive"] is True
    assert client.get(f"/api/projects/{pid}").json()["sensitive"] is True


def _team_app(tmp_path: Path) -> Any:
    from frisket.team.app import TeamConfig, create_team_app

    async def send(_email: str, _link: str) -> bool:
        return True

    return create_team_app(
        TeamConfig(
            database_url=f"sqlite:///{tmp_path / 'control.db'}",
            # create_team_app now requires a run-queue locator
            # unconditionally (deferred follow-up from the queue-composition audit,
            # "general run-queue mismatch", completed).
            run_queue_database_url=f"sqlite:///{tmp_path / 'run-queue.db'}",
            data_dir=tmp_path / "data",
            base_url="http://testserver",
            organization_name="One Org",
            admin_emails={"owner@example.com"},
        ),
        send_magic_email=send,
    )


def _login(client: TestClient, app: Any, email: str) -> None:
    claim_server(app, client=client)
    if email != "owner@example.com":
        seed_member_invite(app, email)
    sign_in_with_magic_link(app, client, email)


def test_team_viewer_is_denied_and_owner_updates_canonical_bundle(
    tmp_path: Path,
) -> None:
    app = _team_app(tmp_path)
    owner, viewer = TestClient(app), TestClient(app)
    _login(owner, app, "owner@example.com")
    _login(viewer, app, "viewer@example.com")
    made = owner.post("/api/projects", json={"name": "Shared", "sensitive": True})
    assert made.status_code == 200, made.text
    assert made.json()["sensitive"] is True
    pid = made.json()["id"]
    granted = owner.post(
        f"/api/projects/{pid}/members",
        json={"email": "viewer@example.com", "role": "viewer"},
    )
    assert granted.status_code == 200, granted.text

    from frisket.team.schema import projects

    def control_plane_sensitive() -> bool:
        with app.state.control_engine.connect() as cx:
            return bool(
                cx.execute(
                    sa.select(projects.c.sensitive).where(projects.c.slug == pid)
                ).scalar_one()
            )

    denied = viewer.patch(f"/api/projects/{pid}/sensitivity", json={"sensitive": True})
    assert denied.status_code == 403, denied.text

    manifest = tmp_path / "data" / f"{pid}.frisket" / "manifest.json"
    # A real transition in both directions, because the route has TWO stores to
    # keep in step: the bundle the egress gate reads, and the control-plane
    # column the hosted surfaces and intent recovery read. Asserting only the
    # bundle let the two diverge permanently for any project whose sensitivity
    # was set after creation.
    for target in (False, True):
        allowed = owner.patch(
            f"/api/projects/{pid}/sensitivity", json={"sensitive": target}
        )
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["sensitive"] is target
        assert json.loads(manifest.read_text())["sensitive"] is target
        assert control_plane_sensitive() is target


def test_team_sensitive_create_intent_recovers_without_downgrading_or_losing_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash between the durable intent and bundle completion preserves intent.

    The intent is the source of truth until it is atomically projected into the
    bundle/control plane.  In particular, a sensitive request cannot first
    create a false bundle and only then be flipped true by the request handler:
    a process crash in that window would permit automated egress on restart.
    """

    import frisket.team.app as team_app
    from frisket.team.schema import (
        audit_log,
        project_creation_intents,
        project_roles,
        projects,
    )

    app = _team_app(tmp_path)
    client = TestClient(app, raise_server_exceptions=False)
    _login(client, app, "owner@example.com")
    original_complete = team_app._complete_project_intent

    def crash_after_intent(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("simulated process crash before project completion")

    monkeypatch.setattr(team_app, "_complete_project_intent", crash_after_intent)
    crashed = client.post(
        "/api/projects", json={"name": "Crash-safe sources", "sensitive": True}
    )
    assert crashed.status_code == 500

    with app.state.control_engine.connect() as cx:
        intent = cx.execute(sa.select(project_creation_intents)).one()
    assert intent.sensitive is True
    slug = str(intent.slug)

    # A fresh composition must complete, rather than discard, the durable
    # request.  Restore the real function before the fresh app's startup loop.
    monkeypatch.setattr(team_app, "_complete_project_intent", original_complete)
    recovered = _team_app(tmp_path)
    manifest = tmp_path / "data" / f"{slug}.frisket" / "manifest.json"
    assert json.loads(manifest.read_text())["sensitive"] is True
    with recovered.state.control_engine.connect() as cx:
        projection = cx.execute(
            sa.select(projects.c.sensitive).where(projects.c.slug == slug)
        ).scalar_one()
        owner_count = cx.execute(
            sa.select(sa.func.count())
            .select_from(project_roles)
            .where(project_roles.c.slug == slug, project_roles.c.role == "owner")
        ).scalar_one()
        creation_audit = cx.execute(
            sa.select(audit_log.c.detail).where(
                audit_log.c.action == "project_created",
                audit_log.c.detail == f"{slug}:sensitive=true",
            )
        ).scalar_one_or_none()
        remaining_intents = cx.execute(
            sa.select(sa.func.count())
            .select_from(project_creation_intents)
            .where(project_creation_intents.c.slug == slug)
        ).scalar_one()
    assert projection is True
    assert owner_count == 1
    assert creation_audit == f"{slug}:sensitive=true"
    assert remaining_intents == 0


def test_sensitive_blocks_telemetry_and_contribution_before_payload_build(
    tmp_path: Path,
) -> None:
    Contributor, _ = _correction_api()
    workspace = tmp_path / "workspace"
    destination = _Destination()
    app = create_app(workspace, product_telemetry_destination=destination)
    client = TestClient(app)
    made = client.post("/api/projects", json={"name": "Protected", "sensitive": True})
    assert made.status_code == 200
    pid = made.json()["id"]
    project = app.state.workspace.get(pid)
    try:
        sender = _CorrectionSender()
        contributor = Contributor(
            enabled=True, sender=sender, project_lookup=lambda _pid: project
        )
        contribution_builds = 0

        def build_pairs() -> list[dict[str, str]]:
            nonlocal contribution_builds
            contribution_builds += 1
            return [{"before": "raw before", "after": "raw after"}]

        response = client.post(
            f"/api/projects/{pid}/telemetry/events",
            json={"monthly_id": "a" * 64, "type": "Project.opened", "properties": {}},
        )
        assert response.status_code == 204
        assert (
            contributor.contribute(project_id="p", pairs_factory=build_pairs) is False
        )
        assert contribution_builds == 0
        assert destination.attempts == []
        assert sender.attempts == []
    finally:
        project.close()


def test_raw_content_is_unexpressible_at_the_project_route(
    tmp_path: Path,
) -> None:
    destination = _Destination()
    client = TestClient(
        create_app(tmp_path / "workspace", product_telemetry_destination=destination)
    )
    pid = client.post("/api/projects", json={"name": "Ordinary"}).json()["id"]
    response = client.post(
        f"/api/projects/{pid}/telemetry/events",
        json={
            "monthly_id": "a" * 64,
            "type": "Project.opened",
            "properties": {"raw_content": "raw document contents"},
        },
    )
    assert response.status_code == 422
    assert destination.attempts == []


def test_turning_sensitive_off_does_not_opt_in_and_offline_export_is_truthful(
    tmp_path: Path,
) -> None:
    Contributor, TrainingExporter = _correction_api()
    project = Project.create(tmp_path / "toggle.frisket", name="Toggle")
    destination, sender = _Destination(), _CorrectionSender()
    try:
        project.set_project_sensitivity(True)
        project.set_meta("truthful_fixture", "before and after remain user-owned")

        # This explicit, user-owned local action remains available even while
        # the project is sensitive; it is not an outbound contribution.
        training_path = tmp_path / "corrections.jsonl"
        TrainingExporter(project_lookup=lambda _pid: project).export(
            project_id="p",
            pairs_factory=lambda: [
                {
                    "before": "original spelling",
                    "after": "corrected spelling",
                }
            ],
            target=training_path,
        )
        assert json.loads(training_path.read_text().strip()) == {
            "before": "original spelling",
            "after": "corrected spelling",
        }
        assert sender.attempts == []

        project.set_project_sensitivity(False)

        contributor = Contributor(
            enabled=False, sender=sender, project_lookup=lambda _pid: project
        )
        assert contributor.contribute(project_id="p", pairs_factory=lambda: []) is False
        assert destination.attempts == sender.attempts == []

        # A deliberate user-owned bundle export is neither telemetry nor a
        # Frisket contribution and remains byte/content truthful.
        archive = project.export(tmp_path / "toggle.frisket.zip")
        restored = Project.import_bundle(archive, tmp_path / "copy.frisket")
        try:
            assert restored.get_meta("truthful_fixture") == (
                "before and after remain user-owned"
            )
            assert restored.project_metadata()["sensitive"] is False
        finally:
            restored.close()
    finally:
        project.close()
