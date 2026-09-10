"""Invocation-bound query primitive for plain callable actions."""

from __future__ import annotations

from typing import Any, Mapping

from frisket.actions.types import QueryPreviewResult
from frisket.contracts.action import ReceiptEvidence, ReceiptIO
from frisket.preview.query import (
    query_preview_rowset_ref,
    resolve_query_preview_resolution,
    revalidate_query_preview_resolution,
)


class QueryPreviewCapability:
    def __init__(self, invocation: Any):
        self._invocation = invocation

    def preview(
        self, *, query: Mapping[str, Any], limit: int, offset: int
    ) -> QueryPreviewResult:
        invocation = self._invocation
        invocation.context.check_cancelled()
        resolved = resolve_query_preview_resolution(
            invocation.project,
            dict(query),
            limit=limit,
            offset=offset,
        )
        revalidate_query_preview_resolution(invocation.project, resolved)
        output = resolved.output
        rowset = query_preview_rowset_ref(output)
        invocation.record(
            inputs=[
                ReceiptIO(
                    name="query",
                    ref={
                        "kind": "query_spec",
                        "schema_version": output.query["schema_version"],
                        "query": output.query,
                        "query_hash": output.query_hash,
                        "sheet_id": output.sheet_id,
                    },
                )
            ],
            outputs=[
                ReceiptIO(name="query_preview", ref={**rowset, "kind": "query_preview"})
            ],
            evidence=[ReceiptEvidence(ref=rowset)],
            provider_use=list(resolved.provider_use),
            before_commit=lambda: revalidate_query_preview_resolution(
                invocation.project, resolved
            ),
        )
        return output
