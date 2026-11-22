"""Data-preserving project upgrade from consents.quote_json to consents.estimate_json."""

from __future__ import annotations

from contextlib import closing
import json

from frisket.engine.store import Project
from frisket.engine.store.execution_routes import ConsentRegistry, RouteStore
from frisket.engine.store.schema import SCHEMA_DIGEST, SCHEMA_DIGEST_META_KEY
from frisket.upgrades.project.edit_precedence_migration import COPY_PRECEDENCE_TO_DIGEST
from tests.engine.test_citations_migration import _downgrade_to_pre_citations_schema


def test_consents_quote_json_survives_upgrade_as_estimate_json(tmp_path) -> None:
    path = tmp_path / "project"
    estimate_payload = json.dumps(
        {"cost": 0.015, "billed_cost": 15000, "policy_id": "test-policy"},
        sort_keys=True,
    )

    with closing(Project.create(path)) as project:
        db = project.db
        db.execute("BEGIN")
        db.execute("ALTER TABLE consents RENAME COLUMN estimate_json TO quote_json")
        db.execute(
            "ALTER TABLE project_qa_research_runs "
            "RENAME COLUMN pending_pause_json TO pending_approval_json"
        )
        db.execute(
            "INSERT INTO consents (id, subject_kind, subject_id, "
            "action_identity_hash, promise_set_hash, standing_policy, "
            "actor, granted_at, grant_basis, quote_json) "
            "VALUES (?,?,?,?,?,NULL,?,?,?,?)",
            (
                "consent_rated_1",
                "run",
                "42",
                "action-id-hash",
                "promise-set-hash",
                "actor-1",
                "2026-11-21T00:00:00Z",
                "user_confirmation",
                estimate_payload,
            ),
        )
        db.execute(
            "INSERT INTO consents (id, subject_kind, subject_id, "
            "action_identity_hash, promise_set_hash, standing_policy, "
            "actor, granted_at, policy_params_json, grant_basis, quote_json) "
            "VALUES (?,NULL,NULL,NULL,NULL,?,?,?,?,NULL,NULL)",
            (
                "consent_standing_1",
                "auto_below_threshold",
                "actor-1",
                "2026-11-21T00:00:00Z",
                '{"threshold_usd": 1.0}',
            ),
        )
        db.commit()
        _downgrade_to_pre_citations_schema(db)
        db.execute("BEGIN")
        db.execute(
            "UPDATE meta SET value=? WHERE key=?",
            (COPY_PRECEDENCE_TO_DIGEST, SCHEMA_DIGEST_META_KEY),
        )
        db.commit()

    with closing(Project(path)) as reopened:
        assert (
            reopened.db.execute(
                "SELECT value FROM meta WHERE key=?", (SCHEMA_DIGEST_META_KEY,)
            ).fetchone()[0]
            == SCHEMA_DIGEST
        )
        columns = {
            row["name"]
            for row in reopened.db.execute("PRAGMA table_info(consents)").fetchall()
        }
        assert "estimate_json" in columns
        assert "quote_json" not in columns

        run_consents = RouteStore.for_run(reopened, 42).consents()
        assert len(run_consents) == 1
        assert run_consents[0].id == "consent_rated_1"
        assert run_consents[0].estimate_json == estimate_payload

        standing_consents = ConsentRegistry(reopened).standing_consents()
        migrated_standing = next(
            row for row in standing_consents if row.id == "consent_standing_1"
        )
        assert migrated_standing.estimate_json is None
