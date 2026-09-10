from __future__ import annotations

import json

from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.engine.store import Project


def test_project_retention_policy_is_explicit_and_blocks_default_compaction(
    tmp_path,
) -> None:
    client = TestClient(create_app(tmp_path / "ws"))
    created = client.post("/api/projects", json={"name": "Retention"}).json()
    pid = created["id"]
    project_path = tmp_path / "ws" / f"{pid}.frisket"

    manifest = json.loads((project_path / "manifest.json").read_text())
    assert manifest["schema_version"] == "frisket.project.v1"
    assert manifest["retention"] == {
        "default_evidence": "compactable",
        "pin_evidence_by_default": False,
        "no_compact": False,
    }

    initial = client.get(f"/api/projects/{pid}/retention")
    assert initial.status_code == 200
    assert initial.json() == {
        "schemaVersion": "frisket.project_retention_policy.v1",
        **manifest["retention"],
        "supported_default_evidence": ["compactable", "pinned", "materialized"],
    }

    updated = client.patch(
        f"/api/projects/{pid}/retention",
        json={"pin_evidence_by_default": True, "no_compact": True},
    )
    assert updated.status_code == 200
    assert updated.json() == {
        "schemaVersion": "frisket.project_retention_policy.v1",
        "default_evidence": "compactable",
        "pin_evidence_by_default": True,
        "no_compact": True,
        "supported_default_evidence": ["compactable", "pinned", "materialized"],
    }

    reopened = Project(project_path)
    assert reopened.retention_policy() == {
        "default_evidence": "compactable",
        "pin_evidence_by_default": True,
        "no_compact": True,
    }
    assert json.loads((project_path / "manifest.json").read_text())["retention"] == {
        "default_evidence": "compactable",
        "pin_evidence_by_default": True,
        "no_compact": True,
    }

    skipped = reopened.compact(vacuum=False)
    assert skipped == {
        "skipped": True,
        "reason": "project_no_compact",
        "results_pruned": 0,
        "blobs_removed": 0,
        "bytes_freed": 0,
        "db_bytes_before": skipped["db_bytes_before"],
        "db_bytes_after": skipped["db_bytes_before"],
        "db_bytes_reclaimed": 0,
    }

    forced = reopened.compact(vacuum=False, force=True)
    assert forced["skipped"] is False


def test_unreadable_retention_policy_fails_closed_and_is_never_rewritten(
    tmp_path,
) -> None:
    """An unparseable retention policy must not silently become permissive.

    The old behavior reset the value to DEFAULT_RETENTION_POLICY *and
    persisted it*, so a corrupt meta row re-enabled compaction of evidence the
    user had explicitly pinned and destroyed the record that they ever chose
    otherwise. Retention gates a destructive operation; it fails closed.
    """
    project_path = tmp_path / "corrupt.frisket"
    project = Project.create(project_path, name="Corrupt")
    try:
        project.set_retention_policy(default_evidence="pinned", no_compact=True)
        project.db.execute(
            "INSERT INTO meta (key, value) VALUES ('retention_policy', '{oops') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        project.db.commit()

        policy = project.retention_policy()
        assert policy["no_compact"] is True
        assert policy["default_evidence"] == "pinned"
        assert policy["pin_evidence_by_default"] is True

        # the destructive operation refuses
        assert project.compact(vacuum=False)["skipped"] is True

        # ...and the unparseable value is preserved, not overwritten
        stored = project.db.execute(
            "SELECT value FROM meta WHERE key='retention_policy'"
        ).fetchone()[0]
        assert stored == "{oops"
    finally:
        project.close()

    # a reopen does not heal-and-persist it either
    reopened = Project(project_path)
    try:
        assert reopened.retention_policy()["no_compact"] is True
        assert (
            reopened.db.execute(
                "SELECT value FROM meta WHERE key='retention_policy'"
            ).fetchone()[0]
            == "{oops"
        )
        # the repair path still works: setting the policy explicitly persists
        # a readable value again.
        reopened.set_retention_policy(no_compact=False)
        assert (
            json.loads(
                reopened.db.execute(
                    "SELECT value FROM meta WHERE key='retention_policy'"
                ).fetchone()[0]
            )["no_compact"]
            is False
        )
    finally:
        reopened.close()
