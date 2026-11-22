"""Canonical embedding metadata in project.db.

``embedding_spaces`` and ``embedding_indexes`` are definitions/provenance, not
vectors. ``EmbeddingStore`` is a thin helper over a ``Project``'s connection:
spaces are minted from a strict descriptor (idempotent by descriptor hash), and
indexes reference a space plus their source/maintenance/provider policy. Derived
vectors live in the unified retrieval sidecar.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from frisket.engine.store import Project
from frisket.engine.store.project import ProjectReadSnapshot
from frisket.engine.store.leases import acquire_lease, release_lease
from frisket.engine.store.transactions import require_transaction, write_transaction

from .spaces import (
    canonical_json,
    current_space_descriptor,
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


@dataclass(frozen=True)
class IndexCountUpdate:
    """Fields to apply to one ``embedding_indexes`` row."""

    total: int | None = None
    ready: int | None = None
    stale: int | None = None
    error: int | None = None
    status: str | None = None
    last_refresh_receipt_id: str | None = None
    last_refresh_job_id: str | None = None
    touch_refreshed_at: bool = True


class EmbeddingStore:
    def __init__(self, project: Project | ProjectReadSnapshot):
        self.project = project

    @property
    def db(self) -> sqlite3.Connection:
        """The CALLING thread's connection, resolved per access — see
        ``RunResultStore.db``: capturing ``project.db`` in ``__init__`` hands
        every thread the CONSTRUCTING thread's connection, which races
        sqlite3's statement cache."""
        return self.project.db

    def create_space_in_transaction(self, descriptor: dict[str, Any]) -> str:
        """Mint (or return the existing) space for a strict descriptor inside
        the caller's open transaction. Idempotent on the descriptor hash — the
        same comparability contract is one row."""
        validate_descriptor(descriptor)
        require_transaction(self.db, "embedding space")
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
        return sid

    def create_space(self, descriptor: dict[str, Any]) -> str:
        """Mint (or return the existing) space for a strict descriptor in its
        own write transaction."""
        validate_descriptor(descriptor)
        with write_transaction(self.db, what="embedding space"):
            return self.create_space_in_transaction(descriptor)

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
    ) -> str:
        """Create an index referencing an existing space, in the caller's
        transaction. Fails loud if the space is unknown (a vector with no
        comparability contract is meaningless)."""
        require_transaction(self.db, "embedding index create")
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
        return idx_id

    def get_index(self, index_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM embedding_indexes WHERE id=?", (index_id,)
        ).fetchone()

    def delete_index(self, index_id: str) -> bool:
        """Delete an index's metadata row in the caller's transaction. Returns
        False if it did not exist. Unified named membership is removed by the
        admitted delete capability."""
        require_transaction(self.db, "embedding index delete")
        cur = self.db.execute("DELETE FROM embedding_indexes WHERE id=?", (index_id,))
        return cur.rowcount > 0

    def update_index_policy(
        self,
        index_id: str,
        *,
        maintenance_policy: dict[str, Any] | None = None,
        provider_policy: dict[str, Any] | None = None,
    ) -> None:
        """Replace the maintenance and/or provider policy JSON on an index, in the
        caller's transaction. A None policy is left unchanged. Metadata-only —
        never touches the space, source scope, or vectors."""
        require_transaction(self.db, "embedding index policy update")
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

    def move_index_to_current_space(self, index_id: str) -> str | None:
        """Point an index whose space predates its provider's current encoding
        generation at the current space, in one owned transaction.

        The index's published members keep their old space, so they read as
        stale and the refresh re-embeds them instead of reusing cached vectors.
        An invalid stored descriptor is left for the readers that report it.
        Returns the new space id, or None when the index did not move."""
        index = self.get_index(index_id)
        space = None if index is None else self.get_space(index["space_id"])
        if space is None:
            return None
        try:
            descriptor = json.loads(space["descriptor_json"])
            validate_descriptor(descriptor)
        except (TypeError, ValueError):
            return None
        current = current_space_descriptor(descriptor)
        if current is descriptor:
            return None
        with write_transaction(
            self.db, what="moving an embedding index to its current space"
        ):
            space_id = self.create_space_in_transaction(current)
            self.db.execute(
                "UPDATE embedding_indexes SET space_id=?, updated_at=datetime('now') "
                "WHERE id=?",
                (space_id, index_id),
            )
        return space_id

    def space_is_referenced(
        self, space_id: str, *, exclude_index_id: str | None = None
    ) -> bool:
        rows = self.db.execute(
            "SELECT COUNT(*) AS n FROM embedding_indexes "
            "WHERE space_id=? AND id IS NOT ?",
            (space_id, exclude_index_id),
        ).fetchone()
        return int(rows["n"]) > 0

    def delete_space_if_orphan(self, space_id: str) -> bool:
        """Delete a space only when no index still references it, in the caller's
        transaction (so it sees an already-deleted index row). Returns True if
        the space row was removed. A shared space survives (scope: do not delete
        a space referenced by other indexes)."""
        require_transaction(self.db, "embedding space delete")
        if self.space_is_referenced(space_id):
            return False
        cur = self.db.execute("DELETE FROM embedding_spaces WHERE id=?", (space_id,))
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

    def update_index_counts_in_transaction(
        self,
        index_id: str,
        counts: IndexCountUpdate,
    ) -> None:
        """Best-effort count/status update inside the caller's open transaction."""
        require_transaction(self.db, "embedding index counts")
        sets: list[str] = ["updated_at=datetime('now')"]
        params: list[Any] = []
        for col, val in (
            ("total_items", counts.total),
            ("ready_items", counts.ready),
            ("stale_items", counts.stale),
            ("error_items", counts.error),
            ("status", counts.status),
            ("last_refresh_receipt_id", counts.last_refresh_receipt_id),
            ("last_refresh_job_id", counts.last_refresh_job_id),
        ):
            if val is not None:
                sets.append(f"{col}=?")
                params.append(val)
        if counts.touch_refreshed_at and (
            counts.status is not None or counts.total is not None
        ):
            sets.append("last_refreshed_at=datetime('now')")
        params.append(index_id)
        self.db.execute(
            f"UPDATE embedding_indexes SET {', '.join(sets)} WHERE id=?", params
        )

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
    ) -> None:
        """Best-effort count/status update (advisory — recomputable from the
        sidecar) in its own write transaction. Only provided fields change."""
        counts = IndexCountUpdate(
            total=total,
            ready=ready,
            stale=stale,
            error=error,
            status=status,
            last_refresh_receipt_id=last_refresh_receipt_id,
            last_refresh_job_id=last_refresh_job_id,
            touch_refreshed_at=touch_refreshed_at,
        )
        with write_transaction(self.db, what="embedding index counts"):
            self.update_index_counts_in_transaction(index_id, counts)
