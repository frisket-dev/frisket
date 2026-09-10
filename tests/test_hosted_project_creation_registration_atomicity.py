"""Red-first proof for hosted-project-creation-registration-atomicity-v1."""

# ruff: noqa: E402 -- private imports must follow the module-level composition skip.

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

hosted_app_module = pytest.importorskip(
    "frisket_cloud.app",
    reason="requires the hosted edition composition",
)
from frisket.server.workspace import Workspace
from frisket.team.identity_service import IdentityAuthService
from frisket.team.schema import audit_log, orgs, project_roles, projects, users
from frisket.team.uow import TeamUnitOfWork
from frisket_cloud.services.litestream_projection import (
    LitestreamProjectLifetime,
    canonical_database_path,
    projection_path,
    projection_root,
)
from hosted_app_test_helpers import HostedConfig, create_hosted_app
from httpx_asgi_test_client import HttpxAsgiTestClient


pytestmark = pytest.mark.gap

_EMAIL = "owner@example.com"
_MEMBER_EMAIL = "member@example.com"
_PROJECT_NAME = "Same"
_ORIGINAL_ALIAS = "same"
_SECOND_ALIAS = "same-2"
_REPLAY_KEY = "hosted-project-create:same:operation-1"
_DISTINCT_KEY = "hosted-project-create:same:operation-2"
_CROSS_ACTOR_KEY = "hosted-project-create:cross-actor:operation-1"
_DELETED_MAPPING_KEY = "hosted-project-create:deleted-mapping:operation-1"
_REPLACED_MAPPING_KEY = "hosted-project-create:replaced-mapping:operation-1"
_REPLACEMENT_KEY = "hosted-project-create:replaced-mapping:operation-2"
_CURRENT_METADATA_KEY = "hosted-project-create:current-metadata:operation-1"
_ROLE_REPLAY_KEY = "hosted-project-create:role-replay:operation-1"
_PAYLOAD_CONSISTENCY_KEY = "hosted-project-create:payload-consistency:operation-1"
_CLOUD_RECEIPT_KEY = "hosted-project-create:cloud-receipt:operation-1"
_CREATION_RECEIPT_ACTION = "hosted_project_creation_registered"


def _sign_in(
    client: HttpxAsgiTestClient,
    engine: sa.Engine,
    *,
    email: str = _EMAIL,
) -> tuple[int, int]:
    with engine.connect() as connection:
        org_id = int(connection.execute(sa.select(orgs.c.id)).scalar_one())
    auth = IdentityAuthService(lambda: TeamUnitOfWork(engine, org_id=org_id))
    token = auth.create_magic_link(email)
    callback = client.get(
        f"/auth/callback?token={token}",
        follow_redirects=False,
    )
    assert callback.status_code == 302, callback.text
    with engine.connect() as connection:
        user = connection.execute(
            sa.select(users.c.id, users.c.default_org_id).where(users.c.email == email)
        ).one()
    assert user.default_org_id is not None
    return int(user.id), int(user.default_org_id)


def _hosted_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, HostedConfig, HttpxAsgiTestClient]:
    monkeypatch.setenv("FRISKET_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("FRISKET_ALLOWED_EMAILS", f"{_EMAIL},{_MEMBER_EMAIL}")
    monkeypatch.setenv("FRISKET_ADMIN_EMAILS", "")
    monkeypatch.setenv("FRISKET_BASE_URL", "http://test")
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("BRAINTRUST_API_KEY", raising=False)
    monkeypatch.setattr(hosted_app_module, "code_version", lambda: "pinned-gap-test")
    config = HostedConfig()
    app = create_hosted_app(config)
    return app, config, HttpxAsgiTestClient(app, raise_server_exceptions=False)


def _post_project(
    client: HttpxAsgiTestClient,
    *,
    operation_key: str,
    name: str = _PROJECT_NAME,
) -> Any:
    return client.post(
        "/api/projects",
        json={"name": name},
        headers={"Idempotency-Key": operation_key},
    )


def _control_state(
    engine: sa.Engine,
    *,
    org_id: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with engine.connect() as connection:
        project_rows = [
            dict(row._mapping)
            for row in connection.execute(
                sa.select(
                    projects.c.id,
                    projects.c.org_id,
                    projects.c.storage_org_id,
                    projects.c.slug,
                    projects.c.name,
                )
                .where(projects.c.org_id == org_id)
                .order_by(projects.c.id)
            )
        ]
        role_rows = [
            dict(row._mapping)
            for row in connection.execute(
                sa.select(
                    project_roles.c.org_id,
                    project_roles.c.slug,
                    project_roles.c.user_id,
                    project_roles.c.role,
                )
                .where(project_roles.c.org_id == org_id)
                .order_by(project_roles.c.slug, project_roles.c.user_id)
            )
        ]
    return project_rows, role_rows


def _control_project_metadata(
    engine: sa.Engine,
    *,
    org_id: int,
    project_id: str,
) -> dict[str, Any]:
    with engine.connect() as connection:
        row = connection.execute(
            sa.select(projects.c.name, projects.c.sensitive).where(
                projects.c.org_id == org_id,
                projects.c.slug == project_id,
            )
        ).one()
    return dict(row._mapping)


def _expected_projection(
    data_dir: Path,
    *,
    org_id: int,
    row: dict[str, Any],
) -> Path:
    return projection_path(
        data_dir,
        LitestreamProjectLifetime(
            project_row_id=int(row["id"]),
            storage_org_id=org_id,
            project_slug=str(row["slug"]),
        ),
    )


def _terminally_delete_project(
    app: Any,
    client: HttpxAsgiTestClient,
    *,
    project_id: str,
    confirm_name: str,
) -> None:
    from test_hosted_project_deletion_grace import (
        _DeletePorts,
        _acknowledge_replica_exclusion,
        _projection_ports,
        _row,
        _wire_delete_ports,
    )

    challenge = client.request(
        "DELETE",
        f"/api/projects/{project_id}",
        json={"confirm_name": confirm_name},
    )
    assert challenge.status_code == 422, challenge.text
    project_row_id = challenge.json()["confirmation"]["project_row_id"]
    requested = client.request(
        "DELETE",
        f"/api/projects/{project_id}",
        json={
            "confirm_name": confirm_name,
            "project_row_id": project_row_id,
        },
    )
    assert requested.status_code == 200, requested.text

    deletions = client.get("/api/project-deletions")
    assert deletions.status_code == 200, deletions.text
    deletion_id = next(
        row["deletion_id"]
        for row in deletions.json()
        if row["project_id"] == project_id
    )
    service = app.state.project_deletion_service
    _wire_delete_ports(
        service,
        _DeletePorts(),
        projection_ports=_projection_ports(
            writer_quiesced=_acknowledge_replica_exclusion,
        ),
    )
    tombstone = _row(app.state.control_engine, deletion_id)
    purged = service.advance_one(
        deletion_id,
        now=tombstone.purge_after.replace(tzinfo=UTC),
    )
    assert purged["status"] == "purged", purged


@pytest.mark.parametrize(
    "failure_phase",
    (
        "bundle_to_control_row",
        "control_row_to_projection",
        "projection_to_owner_role",
    ),
)
def test_operation_replay_completes_one_lifetime_and_new_operation_allocates_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    """Same operation resumes ``same``; a new operation may create ``same-2``."""

    app, config, client = _hosted_app(tmp_path, monkeypatch)
    engine = app.state.control_engine
    access = app.state.project_access_service
    owner_id, org_id = _sign_in(client, engine)
    original_ensure = access.ensure_project_registered
    original_publish = access._publish_project_projection
    injected_failures = 0

    def fail_before_control_row(*_args: Any, **_kwargs: Any) -> int | None:
        nonlocal injected_failures
        injected_failures += 1
        raise RuntimeError("injected failure after bundle creation")

    def fail_around_projection(
        project_row_id: int,
        storage_org_id: int,
        slug: str,
    ) -> None:
        nonlocal injected_failures
        injected_failures += 1
        if failure_phase == "projection_to_owner_role":
            assert original_publish is not None
            original_publish(project_row_id, storage_org_id, slug)
        raise RuntimeError(f"injected failure at {failure_phase}")

    try:
        if failure_phase == "bundle_to_control_row":
            monkeypatch.setattr(
                access, "ensure_project_registered", fail_before_control_row
            )
        else:
            monkeypatch.setattr(
                access, "_publish_project_projection", fail_around_projection
            )

        failed = _post_project(client, operation_key=_REPLAY_KEY)
        assert injected_failures == 1, (
            f"injected {failure_phase} boundary was not reached; "
            f"response={failed.status_code} {failed.text}"
        )

        monkeypatch.setattr(access, "ensure_project_registered", original_ensure)
        monkeypatch.setattr(access, "_publish_project_projection", original_publish)
        retried = _post_project(client, operation_key=_REPLAY_KEY)

        replay_projects, replay_roles = _control_state(engine, org_id=org_id)
        bundle_root = config.data_dir / "projects" / str(org_id)
        replay_bundles = sorted(bundle_root.glob("*.frisket"))
        replay_projections = sorted(
            projection_root(config.data_dir).rglob("project.db")
        )

        distinct = _post_project(client, operation_key=_DISTINCT_KEY)
        final_projects, final_roles = _control_state(engine, org_id=org_id)
        final_bundles = sorted(bundle_root.glob("*.frisket"))
        final_projections = sorted(projection_root(config.data_dir).rglob("project.db"))

        failures: list[str] = []
        retry_alias = (
            str(retried.json().get("id")) if retried.status_code == 200 else None
        )
        if retry_alias != _ORIGINAL_ALIAS:
            failures.append(f"same-key retry returned alias {retry_alias!r}")
        if [row["slug"] for row in replay_projects] != [_ORIGINAL_ALIAS]:
            failures.append("same-key replay did not retain one original control row")
        if replay_bundles != [bundle_root / f"{_ORIGINAL_ALIAS}.frisket"]:
            failures.append("same-key replay did not retain one original bundle")
        expected_replay_roles = [
            {
                "org_id": org_id,
                "slug": _ORIGINAL_ALIAS,
                "user_id": owner_id,
                "role": "owner",
            }
        ]
        if replay_roles != expected_replay_roles:
            failures.append("same-key replay did not retain one original owner role")
        expected_replay_projections = (
            [
                _expected_projection(
                    config.data_dir,
                    org_id=org_id,
                    row=replay_projects[0],
                )
            ]
            if len(replay_projects) == 1
            else []
        )
        if replay_projections != expected_replay_projections:
            failures.append("same-key replay did not retain one original projection")

        distinct_alias = (
            str(distinct.json().get("id")) if distinct.status_code == 200 else None
        )
        if distinct_alias != _SECOND_ALIAS:
            failures.append(f"different-key request returned alias {distinct_alias!r}")
        expected_aliases = [_ORIGINAL_ALIAS, _SECOND_ALIAS]
        final_by_alias = {str(row["slug"]): row for row in final_projects}
        if sorted(final_by_alias) != expected_aliases:
            failures.append(
                "two operation keys did not produce exactly same and same-2"
            )
        if final_bundles != [
            bundle_root / f"{_SECOND_ALIAS}.frisket",
            bundle_root / f"{_ORIGINAL_ALIAS}.frisket",
        ]:
            failures.append("two operation keys did not retain exactly two bundles")
        expected_final_roles = [
            {
                "org_id": org_id,
                "slug": alias,
                "user_id": owner_id,
                "role": "owner",
            }
            for alias in expected_aliases
        ]
        if final_roles != expected_final_roles:
            failures.append("two operation keys did not retain two owner roles")
        expected_final_projections = sorted(
            _expected_projection(
                config.data_dir, org_id=org_id, row=final_by_alias[alias]
            )
            for alias in expected_aliases
            if alias in final_by_alias
        )
        if final_projections != expected_final_projections:
            failures.append("two operation keys did not retain two row-ID projections")
        for alias in expected_aliases:
            row = final_by_alias.get(alias)
            if row is None:
                continue
            projection = _expected_projection(config.data_dir, org_id=org_id, row=row)
            canonical = canonical_database_path(
                config.data_dir,
                LitestreamProjectLifetime(
                    project_row_id=int(row["id"]),
                    storage_org_id=org_id,
                    project_slug=alias,
                ),
            )
            if (
                not projection.is_symlink()
                or projection.resolve() != canonical.resolve()
            ):
                failures.append(
                    f"{alias} projection did not resolve to its canonical DB"
                )

        snapshot = {
            "failure_phase": failure_phase,
            "first_status": failed.status_code,
            "retry_status": retried.status_code,
            "retry_alias": retry_alias,
            "replay_projects": replay_projects,
            "replay_roles": replay_roles,
            "replay_bundles": [path.name for path in replay_bundles],
            "distinct_status": distinct.status_code,
            "distinct_alias": distinct_alias,
            "final_projects": final_projects,
            "final_roles": final_roles,
            "final_bundles": [path.name for path in final_bundles],
        }
        assert not failures, f"{'; '.join(failures)}; state={snapshot!r}"
    finally:
        client.close()
        app.state.run_queue.close()
        engine.dispose()


def test_operation_key_is_scoped_to_the_authenticated_actor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One org member cannot replay another member's creation operation."""

    app, _config, client = _hosted_app(tmp_path, monkeypatch)
    engine = app.state.control_engine
    try:
        owner_id, org_id = _sign_in(client, engine)
        invited = client.post("/api/org/invite", json={"email": _MEMBER_EMAIL})
        assert invited.status_code == 200, invited.text

        created = _post_project(
            client,
            operation_key=_CROSS_ACTOR_KEY,
            name="Owner Project",
        )
        assert created.status_code == 200, created.text
        assert created.json()["id"] == "owner-project"

        client.cookies.clear()
        member_id, member_org_id = _sign_in(
            client,
            engine,
            email=_MEMBER_EMAIL,
        )
        assert member_org_id == org_id
        second = _post_project(
            client,
            operation_key=_CROSS_ACTOR_KEY,
            name="Member Project",
        )

        project_rows, role_rows = _control_state(engine, org_id=org_id)
        snapshot = {
            "first": (created.status_code, created.json()),
            "second": (
                second.status_code,
                second.json() if second.status_code == 200 else second.text,
            ),
            "projects": project_rows,
            "roles": role_rows,
            "owner_id": owner_id,
            "member_id": member_id,
        }
        assert second.status_code == 200, snapshot
        assert second.json()["id"] == "member-project", snapshot
        assert [row["slug"] for row in project_rows] == [
            "owner-project",
            "member-project",
        ], snapshot
        assert role_rows == [
            {
                "org_id": org_id,
                "slug": "member-project",
                "user_id": member_id,
                "role": "owner",
            },
            {
                "org_id": org_id,
                "slug": "owner-project",
                "user_id": owner_id,
                "role": "owner",
            },
        ], snapshot
    finally:
        client.close()
        app.state.run_queue.close()
        engine.dispose()


def test_operation_replay_survives_failure_immediately_after_bundle_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable operation binding recovers the just-created original alias."""

    app, config, client = _hosted_app(tmp_path, monkeypatch)
    engine = app.state.control_engine
    original_create = Workspace.create
    injected_failures = 0

    def create_then_fail(
        workspace: Workspace,
        name: str,
        *,
        project_id: str | None = None,
        sensitive: bool = False,
    ) -> dict[str, Any]:
        nonlocal injected_failures
        project = original_create(
            workspace,
            name,
            project_id=project_id,
            sensitive=sensitive,
        )
        injected_failures += 1
        raise RuntimeError(
            f"injected failure after bundle creation for {project['id']}"
        )

    try:
        owner_id, org_id = _sign_in(client, engine)
        monkeypatch.setattr(Workspace, "create", create_then_fail)
        try:
            failed = _post_project(client, operation_key=_REPLAY_KEY)
        finally:
            monkeypatch.setattr(Workspace, "create", original_create)

        bundle_root = config.data_dir / "projects" / str(org_id)
        bundles_after_failure = sorted(bundle_root.glob("*.frisket"))
        projects_after_failure, roles_after_failure = _control_state(
            engine,
            org_id=org_id,
        )
        projections_after_failure = sorted(
            projection_root(config.data_dir).rglob("project.db")
        )

        retried = _post_project(client, operation_key=_REPLAY_KEY)
        project_rows, role_rows = _control_state(engine, org_id=org_id)
        final_bundles = sorted(bundle_root.glob("*.frisket"))
        final_projections = sorted(projection_root(config.data_dir).rglob("project.db"))
        expected_projection = (
            _expected_projection(
                config.data_dir,
                org_id=org_id,
                row=project_rows[0],
            )
            if len(project_rows) == 1
            else None
        )
        expected_canonical = (
            canonical_database_path(
                config.data_dir,
                LitestreamProjectLifetime(
                    project_row_id=int(project_rows[0]["id"]),
                    storage_org_id=org_id,
                    project_slug=_ORIGINAL_ALIAS,
                ),
            )
            if len(project_rows) == 1
            else None
        )

        failures: list[str] = []
        if injected_failures != 1 or failed.status_code != 500:
            failures.append("post-bundle failure seam did not execute exactly once")
        if bundles_after_failure != [bundle_root / f"{_ORIGINAL_ALIAS}.frisket"]:
            failures.append("failure did not leave exactly the original bundle")
        if projects_after_failure or roles_after_failure or projections_after_failure:
            failures.append("hosted registration advanced after the injected failure")
        retry_alias = (
            str(retried.json().get("id")) if retried.status_code == 200 else None
        )
        if retry_alias != _ORIGINAL_ALIAS:
            failures.append(f"same-operation retry returned alias {retry_alias!r}")
        if [row["slug"] for row in project_rows] != [_ORIGINAL_ALIAS]:
            failures.append("retry did not register only the original alias")
        if role_rows != [
            {
                "org_id": org_id,
                "slug": _ORIGINAL_ALIAS,
                "user_id": owner_id,
                "role": "owner",
            }
        ]:
            failures.append("retry did not bind only the original owner role")
        if final_bundles != [bundle_root / f"{_ORIGINAL_ALIAS}.frisket"]:
            failures.append("retry retained a suffixed or duplicate bundle")
        if expected_projection is None or final_projections != [expected_projection]:
            failures.append("retry did not publish one original-lifetime projection")
        elif (
            not expected_projection.is_symlink()
            or expected_canonical is None
            or expected_projection.resolve() != expected_canonical.resolve()
        ):
            failures.append("original projection did not resolve to its canonical DB")

        snapshot = {
            "injected_failures": injected_failures,
            "failed_status": failed.status_code,
            "bundles_after_failure": [path.name for path in bundles_after_failure],
            "projects_after_failure": projects_after_failure,
            "roles_after_failure": roles_after_failure,
            "projections_after_failure": [
                str(path) for path in projections_after_failure
            ],
            "retry_status": retried.status_code,
            "retry_alias": retry_alias,
            "final_projects": project_rows,
            "final_roles": role_rows,
            "final_bundles": [path.name for path in final_bundles],
        }
        assert not failures, f"{'; '.join(failures)}; state={snapshot!r}"
    finally:
        client.close()
        app.state.run_queue.close()
        engine.dispose()


def test_completed_operation_refuses_replay_after_terminal_project_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A completed mapping cannot resurrect its deliberately deleted lifetime."""

    app, config, client = _hosted_app(tmp_path, monkeypatch)
    engine = app.state.control_engine
    project_name = "Deleted Mapping"
    project_id = "deleted-mapping"
    try:
        _owner_id, org_id = _sign_in(client, engine)
        created = _post_project(
            client,
            operation_key=_DELETED_MAPPING_KEY,
            name=project_name,
        )
        assert created.status_code == 200, created.text
        assert created.json()["id"] == project_id

        _terminally_delete_project(
            app,
            client,
            project_id=project_id,
            confirm_name=project_name,
        )
        bundle = config.data_dir / "projects" / str(org_id) / f"{project_id}.frisket"
        before_projects, before_roles = _control_state(engine, org_id=org_id)
        assert not bundle.exists()
        assert before_projects == []
        assert before_roles == []

        replayed = _post_project(
            client,
            operation_key=_DELETED_MAPPING_KEY,
            name=project_name,
        )
        after_projects, after_roles = _control_state(engine, org_id=org_id)
        snapshot = {
            "status": replayed.status_code,
            "body": replayed.text,
            "projects": after_projects,
            "roles": after_roles,
            "bundle_exists": bundle.exists(),
        }
        assert replayed.status_code == 409, snapshot
        assert after_projects == [], snapshot
        assert after_roles == [], snapshot
        assert not bundle.exists(), snapshot
    finally:
        client.close()
        app.state.run_queue.close()
        engine.dispose()


def test_completed_operation_refuses_a_different_lifetime_at_the_same_slug(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale mapping cannot grant ownership on a replacement lifetime."""

    app, config, client = _hosted_app(tmp_path, monkeypatch)
    engine = app.state.control_engine
    project_name = "Replaced Mapping"
    project_id = "replaced-mapping"
    try:
        owner_id, org_id = _sign_in(client, engine)
        invited = client.post("/api/org/invite", json={"email": _MEMBER_EMAIL})
        assert invited.status_code == 200, invited.text
        original = _post_project(
            client,
            operation_key=_REPLACED_MAPPING_KEY,
            name=project_name,
        )
        assert original.status_code == 200, original.text
        assert original.json()["id"] == project_id

        _terminally_delete_project(
            app,
            client,
            project_id=project_id,
            confirm_name=project_name,
        )
        client.cookies.clear()
        member_id, member_org_id = _sign_in(
            client,
            engine,
            email=_MEMBER_EMAIL,
        )
        assert member_org_id == org_id
        replacement = _post_project(
            client,
            operation_key=_REPLACEMENT_KEY,
            name=project_name,
        )
        assert replacement.status_code == 200, replacement.text
        assert replacement.json()["id"] == project_id

        bundle = config.data_dir / "projects" / str(org_id) / f"{project_id}.frisket"
        replacement_manifest = json.loads((bundle / "manifest.json").read_text())
        before_projects, before_roles = _control_state(engine, org_id=org_id)
        assert [row["slug"] for row in before_projects] == [project_id]
        assert before_roles == [
            {
                "org_id": org_id,
                "slug": project_id,
                "user_id": member_id,
                "role": "owner",
            }
        ]

        client.cookies.clear()
        replay_owner_id, replay_org_id = _sign_in(client, engine)
        assert (replay_owner_id, replay_org_id) == (owner_id, org_id)
        replayed = _post_project(
            client,
            operation_key=_REPLACED_MAPPING_KEY,
            name=project_name,
        )
        after_projects, after_roles = _control_state(engine, org_id=org_id)
        after_manifest = json.loads((bundle / "manifest.json").read_text())
        snapshot = {
            "status": replayed.status_code,
            "body": replayed.text,
            "projects_before": before_projects,
            "projects_after": after_projects,
            "roles_before": before_roles,
            "roles_after": after_roles,
            "replacement_storage_id": replacement_manifest["storage_id"],
            "after_storage_id": after_manifest["storage_id"],
        }
        assert replayed.status_code == 409, snapshot
        assert after_projects == before_projects, snapshot
        assert after_roles == before_roles, snapshot
        assert after_manifest == replacement_manifest, snapshot
    finally:
        client.close()
        app.state.run_queue.close()
        engine.dispose()


def test_completed_operation_replay_preserves_current_project_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creation replay returns current metadata and cannot roll control back."""

    app, config, client = _hosted_app(tmp_path, monkeypatch)
    engine = app.state.control_engine
    original_name = "Current Metadata"
    current_name = "Current Metadata Renamed"
    project_id = "current-metadata"
    try:
        _owner_id, org_id = _sign_in(client, engine)
        created = _post_project(
            client,
            operation_key=_CURRENT_METADATA_KEY,
            name=original_name,
        )
        assert created.status_code == 200, created.text
        assert created.json()["id"] == project_id

        renamed = client.patch(
            f"/api/projects/{project_id}",
            json={"name": current_name},
        )
        assert renamed.status_code == 200, renamed.text
        made_sensitive = client.patch(
            f"/api/projects/{project_id}/sensitivity",
            json={"sensitive": True},
        )
        assert made_sensitive.status_code == 200, made_sensitive.text

        bundle = config.data_dir / "projects" / str(org_id) / f"{project_id}.frisket"
        manifest_before = json.loads((bundle / "manifest.json").read_text())
        control_before = _control_project_metadata(
            engine,
            org_id=org_id,
            project_id=project_id,
        )
        assert manifest_before["name"] == current_name
        assert manifest_before["sensitive"] is True
        assert control_before == {"name": current_name, "sensitive": True}

        replayed = _post_project(
            client,
            operation_key=_CURRENT_METADATA_KEY,
            name=original_name,
        )
        manifest_after = json.loads((bundle / "manifest.json").read_text())
        control_after = _control_project_metadata(
            engine,
            org_id=org_id,
            project_id=project_id,
        )
        snapshot = {
            "status": replayed.status_code,
            "body": replayed.text,
            "manifest_before": manifest_before,
            "manifest_after": manifest_after,
            "control_before": control_before,
            "control_after": control_after,
        }
        assert replayed.status_code == 200, snapshot
        assert replayed.json()["name"] == current_name, snapshot
        assert replayed.json()["sensitive"] is True, snapshot
        assert manifest_after == manifest_before, snapshot
        assert control_after == control_before, snapshot
    finally:
        client.close()
        app.state.run_queue.close()
        engine.dispose()


def test_completed_operation_replay_preserves_newer_project_role(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creation replay cannot restore an owner role changed through the API."""

    app, _config, client = _hosted_app(tmp_path, monkeypatch)
    engine = app.state.control_engine
    project_name = "Role Replay"
    project_id = "role-replay"
    try:
        owner_id, org_id = _sign_in(client, engine)
        invited = client.post("/api/org/invite", json={"email": _MEMBER_EMAIL})
        assert invited.status_code == 200, invited.text

        client.cookies.clear()
        member_id, member_org_id = _sign_in(
            client,
            engine,
            email=_MEMBER_EMAIL,
        )
        assert member_org_id == org_id
        created = _post_project(
            client,
            operation_key=_ROLE_REPLAY_KEY,
            name=project_name,
        )
        assert created.status_code == 200, created.text
        assert created.json()["id"] == project_id
        granted_owner = client.post(
            f"/api/projects/{project_id}/members",
            json={"email": _EMAIL, "role": "owner"},
        )
        assert granted_owner.status_code == 200, granted_owner.text

        client.cookies.clear()
        replay_owner_id, replay_org_id = _sign_in(client, engine)
        assert (replay_owner_id, replay_org_id) == (owner_id, org_id)
        changed = client.post(
            f"/api/projects/{project_id}/members",
            json={"email": _MEMBER_EMAIL, "role": "viewer"},
        )
        assert changed.status_code == 200, changed.text
        before_projects, before_roles = _control_state(engine, org_id=org_id)
        assert before_roles == [
            {
                "org_id": org_id,
                "slug": project_id,
                "user_id": owner_id,
                "role": "owner",
            },
            {
                "org_id": org_id,
                "slug": project_id,
                "user_id": member_id,
                "role": "viewer",
            },
        ]

        client.cookies.clear()
        replay_member_id, replay_member_org_id = _sign_in(
            client,
            engine,
            email=_MEMBER_EMAIL,
        )
        assert (replay_member_id, replay_member_org_id) == (member_id, org_id)
        replayed = _post_project(
            client,
            operation_key=_ROLE_REPLAY_KEY,
            name=project_name,
        )
        after_projects, after_roles = _control_state(engine, org_id=org_id)
        snapshot = {
            "status": replayed.status_code,
            "body": replayed.text,
            "projects_before": before_projects,
            "projects_after": after_projects,
            "roles_before": before_roles,
            "roles_after": after_roles,
        }
        assert replayed.status_code == 200, snapshot
        assert after_projects == before_projects, snapshot
        assert after_roles == before_roles, snapshot
    finally:
        client.close()
        app.state.run_queue.close()
        engine.dispose()


def test_operation_key_rejects_a_different_creation_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One actor's operation key cannot be reused for different inputs."""

    app, config, client = _hosted_app(tmp_path, monkeypatch)
    engine = app.state.control_engine
    project_name = "Payload Consistency"
    project_id = "payload-consistency"
    try:
        owner_id, org_id = _sign_in(client, engine)
        created = client.post(
            "/api/projects",
            json={"name": project_name, "sensitive": True},
            headers={"Idempotency-Key": _PAYLOAD_CONSISTENCY_KEY},
        )
        assert created.status_code == 200, created.text
        assert created.json()["id"] == project_id
        assert created.json()["sensitive"] is True

        equivalent = client.post(
            "/api/projects",
            json={"sensitive": True, "name": project_name},
            headers={"Idempotency-Key": _PAYLOAD_CONSISTENCY_KEY},
        )
        assert equivalent.status_code == 200, equivalent.text
        assert equivalent.json()["id"] == project_id
        assert equivalent.json()["name"] == project_name
        assert equivalent.json()["sensitive"] is True

        bundle = config.data_dir / "projects" / str(org_id) / f"{project_id}.frisket"
        manifest_before = json.loads((bundle / "manifest.json").read_text())
        projects_before, roles_before = _control_state(engine, org_id=org_id)
        metadata_before = _control_project_metadata(
            engine,
            org_id=org_id,
            project_id=project_id,
        )
        bundles_before = sorted(path.name for path in bundle.parent.glob("*.frisket"))
        assert [row["slug"] for row in projects_before] == [project_id]
        assert metadata_before == {"name": project_name, "sensitive": True}
        assert roles_before == [
            {
                "org_id": org_id,
                "slug": project_id,
                "user_id": owner_id,
                "role": "owner",
            }
        ]
        assert bundles_before == [f"{project_id}.frisket"]

        conflicting = client.post(
            "/api/projects",
            json={"name": "Changed Payload", "sensitive": False},
            headers={"Idempotency-Key": _PAYLOAD_CONSISTENCY_KEY},
        )
        manifest_after = json.loads((bundle / "manifest.json").read_text())
        projects_after, roles_after = _control_state(engine, org_id=org_id)
        metadata_after = _control_project_metadata(
            engine,
            org_id=org_id,
            project_id=project_id,
        )
        bundles_after = sorted(path.name for path in bundle.parent.glob("*.frisket"))

        failures: list[str] = []
        if conflicting.status_code != 409:
            failures.append(
                f"changed payload returned {conflicting.status_code}, expected 409"
            )
        if manifest_after != manifest_before:
            failures.append("changed payload mutated the original bundle manifest")
        if projects_after != projects_before or metadata_after != metadata_before:
            failures.append("changed payload mutated hosted project state")
        if roles_after != roles_before:
            failures.append("changed payload mutated the original owner role")
        if bundles_after != bundles_before:
            failures.append("changed payload allocated or removed a bundle")

        snapshot = {
            "created": (created.status_code, created.json()),
            "equivalent": (equivalent.status_code, equivalent.json()),
            "conflicting": (conflicting.status_code, conflicting.text),
            "manifest_before": manifest_before,
            "manifest_after": manifest_after,
            "projects_before": projects_before,
            "projects_after": projects_after,
            "metadata_before": metadata_before,
            "metadata_after": metadata_after,
            "roles_before": roles_before,
            "roles_after": roles_after,
            "bundles_before": bundles_before,
            "bundles_after": bundles_after,
        }
        assert not failures, f"{'; '.join(failures)}; state={snapshot!r}"
    finally:
        client.close()
        app.state.run_queue.close()
        engine.dispose()


def test_operation_replay_refuses_a_conflicting_cloud_completion_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Cloud receipt conflict must replace Base's successful replay."""

    app, config, client = _hosted_app(tmp_path, monkeypatch)
    engine = app.state.control_engine
    project_name = "Cloud Receipt"
    project_id = "cloud-receipt"
    try:
        owner_id, org_id = _sign_in(client, engine)
        created = _post_project(
            client,
            operation_key=_CLOUD_RECEIPT_KEY,
            name=project_name,
        )
        assert created.status_code == 200, created.text
        assert created.json()["id"] == project_id

        with engine.begin() as connection:
            receipt = connection.execute(
                sa.select(audit_log.c.id, audit_log.c.detail).where(
                    audit_log.c.user_id == owner_id,
                    audit_log.c.org_id == org_id,
                    audit_log.c.action == _CREATION_RECEIPT_ACTION,
                )
            ).one()
            assert isinstance(receipt.detail, str)
            receipt_parts = receipt.detail.split("|", 4)
            assert len(receipt_parts) == 5
            assert receipt_parts[0] == "v1"
            assert receipt_parts[3] == project_id
            altered_receipt = "|".join(
                [
                    receipt_parts[0],
                    receipt_parts[1],
                    receipt_parts[2],
                    "different-project-lifetime",
                    receipt_parts[4],
                ]
            )
            updated = connection.execute(
                audit_log.update()
                .where(audit_log.c.id == receipt.id)
                .values(detail=altered_receipt)
            )
            assert updated.rowcount == 1

        bundle = config.data_dir / "projects" / str(org_id) / f"{project_id}.frisket"
        manifest_before = json.loads((bundle / "manifest.json").read_text())
        projects_before, roles_before = _control_state(engine, org_id=org_id)
        metadata_before = _control_project_metadata(
            engine,
            org_id=org_id,
            project_id=project_id,
        )
        bundles_before = sorted(path.name for path in bundle.parent.glob("*.frisket"))
        assert [row["slug"] for row in projects_before] == [project_id]
        assert roles_before == [
            {
                "org_id": org_id,
                "slug": project_id,
                "user_id": owner_id,
                "role": "owner",
            }
        ]
        assert bundles_before == [f"{project_id}.frisket"]

        replayed = _post_project(
            client,
            operation_key=_CLOUD_RECEIPT_KEY,
            name=project_name,
        )
        manifest_after = json.loads((bundle / "manifest.json").read_text())
        projects_after, roles_after = _control_state(engine, org_id=org_id)
        metadata_after = _control_project_metadata(
            engine,
            org_id=org_id,
            project_id=project_id,
        )
        bundles_after = sorted(path.name for path in bundle.parent.glob("*.frisket"))

        failures: list[str] = []
        if replayed.status_code != 409:
            failures.append(
                f"conflicting Cloud receipt returned {replayed.status_code}, expected 409"
            )
        if manifest_after != manifest_before:
            failures.append("receipt conflict mutated the public bundle manifest")
        if projects_after != projects_before or metadata_after != metadata_before:
            failures.append("receipt conflict mutated the hosted project row")
        if roles_after != roles_before:
            failures.append("receipt conflict mutated the project role")
        if bundles_after != bundles_before:
            failures.append("receipt conflict allocated or removed a bundle")

        snapshot = {
            "created": (created.status_code, created.json()),
            "replayed": (replayed.status_code, replayed.text),
            "altered_receipt": altered_receipt,
            "manifest_before": manifest_before,
            "manifest_after": manifest_after,
            "projects_before": projects_before,
            "projects_after": projects_after,
            "metadata_before": metadata_before,
            "metadata_after": metadata_after,
            "roles_before": roles_before,
            "roles_after": roles_after,
            "bundles_before": bundles_before,
            "bundles_after": bundles_after,
        }
        assert not failures, f"{'; '.join(failures)}; state={snapshot!r}"
    finally:
        client.close()
        app.state.run_queue.close()
        engine.dispose()
