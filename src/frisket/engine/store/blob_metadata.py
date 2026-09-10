"""Transactional storage for namespaced blob metadata documents.

Blob metadata has several independent writers (acquisition, probes, and
derived caches). Every read-modify-write must take the SQLite writer lock
*before* reading the document or two otherwise-correct writers can silently
replace each other's keys.  This module is intentionally media-agnostic; an
owned namespace supplies its own compare-and-swap policy through a resolver.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


BLOB_METADATA_MAX_JSON_BYTES = 4 * 1024 * 1024
BLOB_METADATA_MAX_JSON_DEPTH = 64


def validate_json_text_bounds(
    value: Any,
    *,
    max_bytes: int,
    max_depth: int,
    label: str,
) -> str:
    """Reject oversized or deeply nested JSON before asking ``json.loads``.

    The depth scan is deliberately lexical: braces inside strings do not count,
    and the JSON decoder remains responsible for structural validity.  This
    keeps corrupt persisted state from driving an unbounded allocation or a
    Python recursion failure before callers can apply their normal fail-closed
    handling.
    """

    if not isinstance(value, str):
        raise ValueError(f"{label} must be JSON text")
    if len(value) > max_bytes:
        raise ValueError(f"{label} exceeds its byte limit")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8") from exc
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds its byte limit")

    depth = 0
    in_string = False
    escaped = False
    for char in value:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > max_depth:
                raise ValueError(f"{label} exceeds its depth limit")
        elif char in "]}":
            depth = max(0, depth - 1)
    return value


def canonical_blob_metadata_json(value: Mapping[str, Any]) -> str:
    """Encode a portable, deterministic blob metadata object."""

    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except RecursionError as exc:
        raise ValueError("blob metadata exceeds its depth limit") from exc
    return validate_json_text_bounds(
        encoded,
        max_bytes=BLOB_METADATA_MAX_JSON_BYTES,
        max_depth=BLOB_METADATA_MAX_JSON_DEPTH,
        label="blob metadata",
    )


@dataclass(frozen=True)
class BlobMetadataNamespaceCASDecision:
    """Decision returned by a namespace-specific CAS resolver."""

    written: bool
    value: Any
    reason: str


@dataclass(frozen=True)
class BlobMetadataNamespaceCASResult:
    """Detached result of a namespaced metadata compare-and-swap."""

    written: bool
    value: Any
    reason: str


class BlobMetadataDocumentStore:
    """Core locked read-modify-write seam for the ``blobs.metadata`` object."""

    def __init__(self, project: Any):
        self.project = project

    @property
    def db(self) -> Any:
        """The CALLING thread's connection, resolved per access — see
        ``RunResultStore.db``: capturing ``project.db`` in ``__init__`` hands
        every thread the CONSTRUCTING thread's connection, which races
        sqlite3's statement cache."""
        return self.project.db

    @staticmethod
    def _decode_document(raw: Any) -> dict[str, Any]:
        text = validate_json_text_bounds(
            raw or "{}",
            max_bytes=BLOB_METADATA_MAX_JSON_BYTES,
            max_depth=BLOB_METADATA_MAX_JSON_DEPTH,
            label="blob metadata",
        )
        try:
            document = json.loads(text)
        except (TypeError, json.JSONDecodeError, RecursionError) as exc:
            raise ValueError("blob metadata is not valid JSON") from exc
        if not isinstance(document, dict):
            raise ValueError("blob metadata must be a JSON object")
        return document

    def document(
        self, digest: str, *, tolerate_invalid: bool = False
    ) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT metadata FROM blobs WHERE hash=?", (digest,)
        ).fetchone()
        if row is None:
            raise KeyError(f"no blob {digest}")
        try:
            return self._decode_document(row["metadata"])
        except ValueError:
            if tolerate_invalid:
                return {}
            raise

    def transactionally_update_document(
        self,
        digest: str,
        update: Callable[[dict[str, Any]], Mapping[str, Any]],
        *,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Update the complete document under a lock acquired before its read.

        When a surrounding transaction already exists, this method joins it.
        ``commit=False`` leaves a transaction opened by this call available to
        its caller, matching the store's existing caller-owned transaction
        convention.
        """

        owns_transaction = not self.db.in_transaction
        try:
            if owns_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT metadata FROM blobs WHERE hash=?", (digest,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no blob {digest}")
            current = self._decode_document(row["metadata"])
            proposed = update(current)
            if not isinstance(proposed, Mapping):
                raise TypeError("blob metadata update must return an object")
            encoded = canonical_blob_metadata_json(proposed)
            cur = self.db.execute(
                "UPDATE blobs SET metadata=? WHERE hash=?", (encoded, digest)
            )
            if cur.rowcount != 1:  # pragma: no cover - protected by write lock
                raise KeyError(f"no blob {digest}")
            if owns_transaction and commit:
                self.db.commit()
            return self._decode_document(encoded)
        except BaseException:
            if owns_transaction and self.db.in_transaction:
                self.db.rollback()
            raise

    def compare_and_swap_metadata_namespace(
        self,
        digest: str,
        namespace: str,
        proposal: Any,
        resolve: Callable[[Any, Any], BlobMetadataNamespaceCASDecision],
        *,
        commit: bool = True,
    ) -> BlobMetadataNamespaceCASResult:
        """Apply an owned namespace's CAS policy under one locked transaction."""

        if not isinstance(namespace, str) or not namespace:
            raise ValueError("blob metadata namespace must be a non-empty string")
        owns_transaction = not self.db.in_transaction
        try:
            if owns_transaction:
                self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT metadata FROM blobs WHERE hash=?", (digest,)
            ).fetchone()
            if row is None:
                raise KeyError(f"no blob {digest}")
            document = self._decode_document(row["metadata"])
            decision = resolve(document.get(namespace), proposal)
            if not isinstance(decision, BlobMetadataNamespaceCASDecision):
                raise TypeError(
                    "blob metadata CAS resolver returned an invalid decision"
                )
            if not decision.written:
                if owns_transaction:
                    self.db.rollback()
                return BlobMetadataNamespaceCASResult(
                    written=False,
                    value=copy.deepcopy(decision.value),
                    reason=decision.reason,
                )

            document[namespace] = decision.value
            encoded = canonical_blob_metadata_json(document)
            cur = self.db.execute(
                "UPDATE blobs SET metadata=? WHERE hash=?", (encoded, digest)
            )
            if cur.rowcount != 1:  # pragma: no cover - protected by write lock
                raise KeyError(f"no blob {digest}")
            if owns_transaction and commit:
                self.db.commit()
            return BlobMetadataNamespaceCASResult(
                written=True,
                value=copy.deepcopy(decision.value),
                reason=decision.reason,
            )
        except BaseException:
            if owns_transaction and self.db.in_transaction:
                self.db.rollback()
            raise
