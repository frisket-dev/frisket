"""Host admission, external enumeration and exact collection-cap confirmation."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from frisket.actions.types import CollectionItem, RowSource, TableError
from frisket.contracts.action import ActionError
from frisket.engine.executor.embedding_read import TableReadRefused
from frisket.engine.runner.confirmation_context import (
    ParamsScope,
    mint_confirmation_hash,
    scope_confirmation,
)
from frisket.engine.runner.confirmation_echo import refuse_unless_exact_echo
from frisket.features.url_classification import classify_url
from frisket.server.sources.youtube import (
    collection_display_name,
    enumerate_youtube_collection,
)

_ENUMERATOR_PROVIDER: Callable[..., Any] | None = None


def _text_hash(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _load_source_url(project, *, sheet_id, column_id, row_id) -> str:
    if any(
        type(value) is not int or value <= 0 for value in (sheet_id, column_id, row_id)
    ):
        raise TableError(
            "invalid_input_ref", "Source cell IDs must be positive integers"
        )
    sheet = project.db.execute(
        "SELECT id FROM sheets WHERE id=? AND hidden=0", (sheet_id,)
    ).fetchone()
    column = project.db.execute(
        "SELECT id FROM columns WHERE id=? AND sheet_id=?", (column_id, sheet_id)
    ).fetchone()
    if (
        sheet is None
        or column is None
        or row_id not in project.visible_row_ids(sheet_id, [row_id])
    ):
        raise TableError(
            "invalid_input_ref", "Source cell must belong to a visible sheet and row"
        )
    raw = project.get_values(sheet_id, column_id, row_ids=[row_id]).get(row_id)
    if not isinstance(raw, str) or not raw.strip():
        raise TableError("invalid_input_ref", "Source cell does not hold a URL")
    return raw.strip()


def validate_collection_source(project, fact) -> None:
    """Validate the actual source-cell URL without invoking its provider."""
    try:
        url = _load_source_url(
            project,
            sheet_id=fact.get("source_sheet_id"),
            column_id=fact.get("source_column_id"),
            row_id=fact.get("source_row_id"),
        )
    except TableError as error:
        raise TableError(
            "stale_replay", "Collection source cell is missing or changed"
        ) from error
    if _text_hash(url) != fact.get("url_hash"):
        raise TableError("stale_replay", "Collection source cell URL changed")


class AdmittedCollectionReader:
    def __init__(self, project, *, action_kind, request_identity, confirmation):
        self.project = project
        self.action_kind = action_kind
        self.request_identity = request_identity
        self.confirmation = confirmation
        self.sources: set[RowSource] = set()
        self.parent_sheet_id: int | None = None
        self.facts: list[dict[str, Any]] = []
        self.provider_use: list[dict[str, Any]] = []

    def read(
        self, *, sheet_id: int, column_id: int, row_id: int
    ) -> tuple[CollectionItem, ...]:
        if self.parent_sheet_id not in (None, sheet_id):
            raise TableError(
                "invalid_input_ref", "Collection reads must share one parent sheet"
            )
        url = _load_source_url(
            self.project, sheet_id=sheet_id, column_id=column_id, row_id=row_id
        )
        from frisket.authoring.workbench.plugin_runtime_capabilities import (
            enabled_workbench_plugin_ids,
        )

        classification = classify_url(
            url, enabled_plugin_ids=enabled_workbench_plugin_ids(self.project)
        )
        if classification is None or classification.kind != "collection":
            raise TableError(
                "invalid_input_ref",
                "Source URL is not a collection (channel/playlist) URL",
            )
        expansion = classification.expansion
        if expansion is None:
            raise TableError(
                "invalid_input_ref",
                "Collection classification carries no expansion hint",
            )
        try:
            enumeration = enumerate_youtube_collection(
                expansion.enumerator,
                url,
                item_cap=expansion.hard_cap,
                provider=_ENUMERATOR_PROVIDER,
            )
        except ValueError as error:
            raise TableError(
                "invalid_input_ref",
                f"Enumerator {expansion.enumerator!r} cannot expand this URL: {error}",
            ) from error
        except Exception as error:
            raise TableError(
                "provider_error", "Collection enumeration failed"
            ) from error

        count = enumeration.preview_count
        if expansion.hard_cap is not None and count > expansion.hard_cap:
            raise TableError(
                "collection_expand_exceeds_hard_cap",
                "Collection size exceeds the hard cap",
                details={
                    "preview_count": count,
                    "default_cap": expansion.default_cap,
                    "hard_cap": expansion.hard_cap,
                },
            )
        suggested_name = collection_display_name(
            expansion.enumerator, enumeration.identity, url
        )
        item_identities = [
            {"video_id": row.get("video_id"), "url": row.get("url")}
            for row in enumeration.rows
        ]
        details = {
            "preview_count": count,
            "default_cap": expansion.default_cap,
            "hard_cap": expansion.hard_cap,
            "provider": classification.provider,
            "enumerator": expansion.enumerator,
            "source_url": url,
            "collection_identity": enumeration.identity,
            "enumerated_items_hash": _text_hash(
                json.dumps(
                    item_identities,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
            ),
            "suggested_target_name": suggested_name,
        }
        confirmation_hash = mint_confirmation_hash(
            scope_confirmation(
                family_kind=self.action_kind,
                scope=ParamsScope(params=self.request_identity),
                bindings={
                    **details,
                    "source_cell": {
                        "sheet_id": sheet_id,
                        "column_id": column_id,
                        "row_id": row_id,
                    },
                },
            )
        )
        if count > expansion.default_cap:
            refusal = refuse_unless_exact_echo(
                confirmed=self.confirmation is not None,
                echoed_hash=self.confirmation,
                expected_hash=confirmation_hash,
                refuse=lambda: ActionError(
                    code="collection_expand_requires_confirmation",
                    message="Collection count exceeds default_cap; resubmit with the confirmation token",
                    action_kind=self.action_kind,
                    field="confirmation",
                    needs_confirmation=True,
                    details={**details, "promise_set_hash": confirmation_hash},
                ),
            )
            if refusal is not None:
                raise TableReadRefused(refusal)
        source = RowSource(sheet_id=sheet_id, row_id=row_id)
        self.sources.add(source)
        self.parent_sheet_id = sheet_id
        self.facts.extend(
            [
                {
                    "kind": "collection_expand_source_cell",
                    "sheet_id": sheet_id,
                    "column_id": column_id,
                    "row_id": row_id,
                    "url": url,
                },
                {
                    "kind": "collection_expand_source_fingerprint",
                    "source_sheet_id": sheet_id,
                    "source_column_id": column_id,
                    "source_row_id": row_id,
                    "url_hash": _text_hash(url),
                },
                {
                    "kind": "collection_expand_classification",
                    "provider": classification.provider,
                    "matcher_id": classification.matcher_id,
                    "enumerator": expansion.enumerator,
                    "unit": expansion.unit,
                    "identity": enumeration.identity,
                    "suggested_target_name": suggested_name,
                },
                {
                    "kind": "collection_expand_enumeration",
                    "preview_count": count,
                    "materialized_count": len(enumeration.rows),
                    "default_cap": expansion.default_cap,
                    "hard_cap": expansion.hard_cap,
                    "truncated": bool(
                        enumeration.truncated or count > len(enumeration.rows)
                    ),
                    "confirmed": self.confirmation is not None,
                },
            ]
        )
        self.provider_use.append(
            {
                "provider": classification.provider,
                "service": "yt-dlp",
                "enumerator": expansion.enumerator,
                "items_returned": count,
                "items_materialized": len(enumeration.rows),
                "download": False,
                "external_api": True,
                "cost_actual": 0.0,
            }
        )
        return tuple(
            CollectionItem(value=dict(row), source=source) for row in enumeration.rows
        )
