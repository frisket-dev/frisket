"""Canonical embedding metadata in project.db.

``embedding_spaces`` and ``embedding_indexes`` are definitions/provenance, not
vectors. ``EmbeddingStore`` is a thin helper over a ``Project``'s connection:
spaces are minted from a strict descriptor (idempotent by descriptor hash), and
indexes reference a space plus their source/maintenance/provider policy. Raw
vectors are the rebuildable ``VectorBackend``'s job, never here.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.leases import acquire_lease, release_lease

from .spaces import (
    canonical_json,
    descriptor_hash,
    space_id_for,
    validate_descriptor,
)

# Remote provider kinds: their refresh egresses data, so it is gated on an
# explicit provider policy before any embed call. Local kinds run inside the
# operator trust boundary. The set
# itself lives in frisket.models.metadata (one table shared with the router
# and the project network gate); re-exported here for the existing consumers.
from frisket.ai.models.metadata import REMOTE_PROVIDER_KINDS as REMOTE_PROVIDER_KINDS


def _source_policy_hash(policy: dict[str, Any]) -> str:
    import hashlib

    digest = hashlib.sha256(canonical_json(policy).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


class EmbeddingStore:
    def __init__(self, project: Project):
        self.project = project

    @property
    def db(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved per access — see
        ``RunResultStore.db``: capturing ``project.db`` in ``__init__`` hands
        every thread the CONSTRUCTING thread's connection, which races
        sqlite3's statement cache."""
        return self.project.db

    def create_space(self, descriptor: dict[str, Any], *, commit: bool = True) -> str:
        """Mint (or return the existing) space for a strict descriptor. Idempotent
        on the descriptor hash — the same comparability contract is one row.

        ``commit=False`` keeps the INSERT inside the caller's open transaction so
        metadata and a receipt can commit (or roll back) together."""
        validate_descriptor(descriptor)
        sid = space_id_for(descriptor)
        dhash = descriptor_hash(descriptor)
        self.db.execute(
            "INSERT INTO embedding_spaces ("
            "id, descriptor_json, descriptor_hash, provider_id, provider_kind, "
            "requested_model, actual_model_id, model_revision, modality, dimension, "
            "dtype, distance_metric, normalization, vector_options_hash, "
            "source_payload_contract, contract_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (
                sid,
                canonical_json(descriptor),
                dhash,
                descriptor["provider_id"],
                descriptor["provider_kind"],
                descriptor.get("requested_model"),
                descriptor["actual_model_id"],
                descriptor.get("model_revision"),
                descriptor["modality"],
                descriptor["dimension"],
                descriptor["dtype"],
                descriptor["distance_metric"],
                descriptor["normalization"],
                descriptor["vector_options_hash"],
                descriptor["source_payload_contract"],
                descriptor["contract_version"],
            ),
        )
        if commit:
            self.db.commit()
        return sid

    def get_space(self, space_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM embedding_spaces WHERE id=?", (space_id,)
        ).fetchone()

    def create_index(
        self,
        *,
        name: str,
        space_id: str,
        sheet_id: int | None,
        source_query: dict[str, Any],
        source_columns: list[str],
        source_policy: dict[str, Any],
        maintenance_policy: dict[str, Any] | None = None,
        provider_policy: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> str:
        """Create an index referencing an existing space. Fails loud if the space
        is unknown (a vector with no comparability contract is meaningless).

        ``commit=False`` keeps the INSERT inside the caller's transaction."""
        if self.get_space(space_id) is None:
            raise ValueError(f"unknown embedding space: {space_id!r}")
        idx_id = f"embidx_{uuid.uuid4().hex[:20]}"
        maintenance_policy = maintenance_policy or {"mode": "manual"}
        provider_policy = provider_policy or {"allow_remote": False}
        self.db.execute(
            "INSERT INTO embedding_indexes ("
            "id, name, space_id, sheet_id, source_query_json, source_columns_json, "
            "source_policy_json, source_policy_hash, maintenance_policy_json, "
            "provider_policy_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                idx_id,
                name,
                space_id,
                sheet_id,
                canonical_json(source_query),
                canonical_json(source_columns),
                canonical_json(source_policy),
                _source_policy_hash(source_policy),
                canonical_json(maintenance_policy),
                canonical_json(provider_policy),
            ),
        )
        if commit:
            self.db.commit()
        return idx_id

    def get_index(self, index_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM embedding_indexes WHERE id=?", (index_id,)
        ).fetchone()

    def delete_index(self, index_id: str, *, commit: bool = True) -> bool:
        """Delete an index's metadata row. Returns False if it did not exist.
        Sidecar vectors are the VectorBackend's responsibility (caller decides).
        ``commit=False`` keeps the DELETE inside the caller's transaction."""
        cur = self.db.execute("DELETE FROM embedding_indexes WHERE id=?", (index_id,))
        if commit:
            self.db.commit()
        return cur.rowcount > 0

    def update_index_policy(
        self,
        index_id: str,
        *,
        maintenance_policy: dict[str, Any] | None = None,
        provider_policy: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> None:
        """Replace the maintenance and/or provider policy JSON on an index. A None
        policy is left unchanged. Metadata-only — never touches the space, source
        scope, or vectors. ``commit=False`` keeps it in the caller's transaction."""
        sets: list[str] = []
        values: list[Any] = []
        if maintenance_policy is not None:
            sets.append("maintenance_policy_json=?")
            values.append(canonical_json(maintenance_policy))
        if provider_policy is not None:
            sets.append("provider_policy_json=?")
            values.append(canonical_json(provider_policy))
        if not sets:
            return
        sets.append("updated_at=datetime('now')")
        values.append(index_id)
        self.db.execute(
            f"UPDATE embedding_indexes SET {', '.join(sets)} WHERE id=?", values
        )
        if commit:
            self.db.commit()

    def space_is_referenced(
        self, space_id: str, *, exclude_index_id: str | None = None
    ) -> bool:
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM embedding_indexes "
            "WHERE space_id=? AND id IS NOT ?",
            (space_id, exclude_index_id),
        ).fetchone()
        return int(rows["n"]) > 0

    def delete_space_if_orphan(self, space_id: str, *, commit: bool = True) -> bool:
        """Delete a space only when no index still references it. Returns True if
        the space row was removed. A shared space survives (scope: do not delete
        a space referenced by other indexes). ``commit=False`` keeps the DELETE in
        the caller's transaction (so it sees an already-deleted index row)."""
        if self.space_is_referenced(space_id):
            return False
        cur = self.db.execute("DELETE FROM embedding_spaces WHERE id=?", (space_id,))
        if commit:
            self.db.commit()
        return cur.rowcount > 0

    def acquire_refresh_claim(
        self, index_id: str, *, lease_seconds: int = 3600
    ) -> str | None:
        """Compare-and-set an index into status='refreshing' under one writer.

        Returns a claim token, or None when another live refresh already holds the
        index (the caller surfaces embedding_index_busy). A claim whose lease has
        expired is a crashed refresh and is reclaimable."""
        return acquire_lease(
            self.db,
            table="embedding_indexes",
            id_value=index_id,
            token_prefix="embrefresh",
            lease_seconds=lease_seconds,
            held_requires=lambda row: row["status"] == "refreshing",
            acquire_set={"status": "refreshing"},
            set_sql=("updated_at=datetime('now')",),
        )

    def release_refresh_claim(
        self, index_id: str, token: str, *, status: str = "ready"
    ) -> None:
        release_lease(
            self.db,
            table="embedding_indexes",
            id_value=index_id,
            token=token,
            release_set={"status": status},
            set_sql=("updated_at=datetime('now')",),
        )

    def list_indexes(self, sheet_id: int | None = None) -> list[sqlite3.Row]:
        if sheet_id is None:
            return self.db.execute(
                "SELECT * FROM embedding_indexes ORDER BY created_at, id"
            ).fetchall()
        return self.db.execute(
            "SELECT * FROM embedding_indexes WHERE sheet_id=? ORDER BY created_at, id",
            (sheet_id,),
        ).fetchall()

    def get_source_policy(self, index_id: str) -> dict[str, Any]:
        import json

        row = self.get_index(index_id)
        if row is None:
            raise KeyError(index_id)
        return json.loads(row["source_policy_json"])

    def update_index_counts(
        self,
        index_id: str,
        *,
        total: int | None = None,
        ready: int | None = None,
        stale: int | None = None,
        error: int | None = None,
        status: str | None = None,
        last_refresh_receipt_id: str | None = None,
        last_refresh_job_id: str | None = None,
        touch_refreshed_at: bool = True,
        commit: bool = True,
    ) -> None:
        """Best-effort count/status update (advisory — recomputable from the
        sidecar). Only provided fields change. ``commit=False`` keeps the UPDATE
        in the caller's transaction (e.g. the same BEGIN as a receipt insert)."""
        sets: list[str] = ["updated_at=datetime('now')"]
        params: list[Any] = []
        for col, val in (
            ("total_items", total),
            ("ready_items", ready),
            ("stale_items", stale),
            ("error_items", error),
            ("status", status),
            ("last_refresh_receipt_id", last_refresh_receipt_id),
            ("last_refresh_job_id", last_refresh_job_id),
        ):
            if val is not None:
                sets.append(f"{col}=?")
                params.append(val)
        if touch_refreshed_at and (status is not None or total is not None):
            sets.append("last_refreshed_at=datetime('now')")
        params.append(index_id)
        self.db.execute(
            f"UPDATE embedding_indexes SET {', '.join(sets)} WHERE id=?", params
        )
        if commit:
            self.db.commit()
