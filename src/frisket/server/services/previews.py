"""Read-only preview services for local server routes."""

from __future__ import annotations

from typing import Any

from frisket.preview.ocr import (
    OcrComparePreviewError,
    compare_ocr_preview,
)
from frisket.preview.cluster import (
    ClusterPreviewError,
    cluster_preview_payload,
    resolve_cluster_preview,
)
from frisket.preview.column_values import (
    ColumnValuesPreviewError,
    resolve_column_values_preview,
)
from frisket.preview.entity_mention_detail import (
    resolve_entity_mention_documents,
    resolve_entity_mention_occurrences,
)
from frisket.preview.entity_mentions import (
    EntityMentionsPreviewError,
    resolve_entity_mentions_preview,
)
from frisket.preview.replace_rules import (
    ReplaceRulesPreviewError,
    resolve_replace_rules_preview,
)
from frisket.preview.query import (
    QueryPreviewError,
    query_preview_payload,
    resolve_query_preview,
)
from frisket.preview.topic_segmentation import (
    TopicSegmentationCompareError,
    compare_topic_segmentation_scratch,
)
from frisket.preview.translate import (
    TranslateComparePreviewError,
    compare_translate_scratch,
)
from frisket.server.workspace import Workspace


class PreviewRequestError(ValueError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        field: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field
        self.details = dict(details or {})

    def query_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "field": self.field,
        }


class PreviewService:
    def __init__(self, workspace: Workspace):
        self._workspace = workspace

    def query_preview(
        self,
        project_id: str,
        *,
        query: dict[str, Any],
        limit: Any,
        offset: Any,
    ) -> dict[str, Any]:
        try:
            preview = resolve_query_preview(
                self._workspace.get(project_id),
                query,
                limit=limit,
                offset=offset,
            )
        except QueryPreviewError as exc:
            raise PreviewRequestError(
                code=exc.code,
                message=exc.message,
                field=exc.field,
            ) from exc
        return query_preview_payload(preview)

    def cluster_preview(
        self,
        project_id: str,
        *,
        sheet_id: Any,
        input_column: Any,
        method: Any,
        min_size: Any,
        threshold: Any,
        ngram_size: Any,
        key_template: Any = None,
    ) -> dict[str, Any]:
        """Synchronous, receipt-free cluster preview (read-only
        non-action endpoint). Computes duplicate-value groups for the chosen
        method over one column; the returned ``value_hash`` is the staleness
        anchor the commit action validates as ``expected_value_hash``.

        NO model router is resolved. Semantic clustering embeds LOCALLY or
        refuses: passing the router used to let a plain preview POST bill
        OpenAI once per distinct column value with no run, no receipt and no
        spend line behind it. ``resolve_embedder``
        states the contract that made that wrong — an ``allow_remote=True``
        caller "owns surfacing the spend through its cost gate BEFORE any embed
        call" — and a preview, by package contract, has no gate to surface it
        with. Remote embedding belongs to the cost-gated semantic actions."""
        project = self._workspace.get(project_id)
        try:
            preview = resolve_cluster_preview(
                project,
                sheet_id=sheet_id,
                input_column=input_column,
                method=method,
                min_size=min_size,
                threshold=threshold,
                ngram_size=ngram_size,
                key_template=key_template,
            )
        except ClusterPreviewError as exc:
            raise PreviewRequestError(
                code=exc.code,
                message=exc.message,
                field=exc.field,
                details=exc.details,
            ) from exc
        return cluster_preview_payload(preview)

    def column_values_preview(
        self,
        project_id: str,
        *,
        sheet_id: Any,
        input_column: Any,
        search: Any,
        limit: Any,
        offset: Any,
    ) -> dict[str, Any]:
        """Synchronous, receipt-free distinct-values preview for one column.

        Feeds the resolve.substitute / resolve.combine authoring surfaces:
        frequency-sorted ``{value, count}`` rows (searchable and paged)."""
        project = self._workspace.get(project_id)
        try:
            return resolve_column_values_preview(
                project,
                sheet_id=sheet_id,
                input_column=input_column,
                search=search,
                limit=limit,
                offset=offset,
            )
        except ColumnValuesPreviewError as exc:
            raise PreviewRequestError(
                code=exc.code,
                message=exc.message,
                field=exc.field,
                details=exc.details,
            ) from exc

    def entity_mentions_preview(
        self,
        project_id: str,
        *,
        sheet_id: Any,
        column_id: Any,
        search: Any,
        type: Any,  # noqa: A002 - wire field name
        limit: Any,
        offset: Any,
    ) -> dict[str, Any]:
        """Synchronous, receipt-free mention-group preview for one marked
        entity column.

        Feeds the Mentions panel: fingerprint-grouped surface groups with
        exact distinct-row counts, every raw spelling behind each group, and
        the ``entity_eq`` selector that reproduces that count as a grid
        filter."""
        project = self._workspace.get(project_id)
        try:
            return resolve_entity_mentions_preview(
                project,
                sheet_id=sheet_id,
                column_id=column_id,
                search=search,
                type=type,
                limit=limit,
                offset=offset,
            )
        except EntityMentionsPreviewError as exc:
            raise PreviewRequestError(
                code=exc.code,
                message=exc.message,
                field=exc.field,
                details=exc.details,
            ) from exc

    def entity_mention_documents(
        self,
        project_id: str,
        *,
        sheet_id: Any,
        column_id: Any,
        type: Any,  # noqa: A002 - wire field name
        fingerprint: Any,
        text: Any,
        limit: Any,
        offset: Any,
    ) -> dict[str, Any]:
        """Which documents ONE normalized mention appears in, paged.

        The scoped counterpart to entity_mentions_preview above: same stream,
        same row scope, filtered to one group instead of aggregating the whole
        column on every click."""
        project = self._workspace.get(project_id)
        try:
            return resolve_entity_mention_documents(
                project,
                sheet_id=sheet_id,
                column_id=column_id,
                type=type,
                fingerprint=fingerprint,
                text=text,
                limit=limit,
                offset=offset,
            )
        except EntityMentionsPreviewError as exc:
            raise PreviewRequestError(
                code=exc.code,
                message=exc.message,
                field=exc.field,
                details=exc.details,
            ) from exc

    def entity_mention_occurrences(
        self,
        project_id: str,
        *,
        sheet_id: Any,
        row_id: Any,
        column_id: Any,
        type: Any,  # noqa: A002 - wire field name
        fingerprint: Any,
        text: Any,
        limit: Any,
        offset: Any,
        snippet_radius: Any,
    ) -> dict[str, Any]:
        """Where inside ONE document a mention occurs, with context, paged.

        The lazy half of the drill-down: Route A lists the documents cheaply
        from the entity arrays, this resolves one of them through the
        coordinate substrate only when the reader expands it."""
        project = self._workspace.get(project_id)
        try:
            return resolve_entity_mention_occurrences(
                project,
                sheet_id=sheet_id,
                row_id=row_id,
                column_id=column_id,
                type=type,
                fingerprint=fingerprint,
                text=text,
                limit=limit,
                offset=offset,
                snippet_radius=snippet_radius,
            )
        except EntityMentionsPreviewError as exc:
            raise PreviewRequestError(
                code=exc.code,
                message=exc.message,
                field=exc.field,
                details=exc.details,
            ) from exc

    def replace_rules_preview(
        self,
        project_id: str,
        *,
        sheet_id: Any,
        input_column: Any,
        rules: Any,
        unmatched: Any,
        test_value: Any,
    ) -> dict[str, Any]:
        """Synchronous, receipt-free rules evaluation for resolve.replace.

        Shares its rule engine with the commit executor (Python ``re``), so
        the authoring UI's live tester and per-rule match counts always agree
        with what Apply writes."""
        project = self._workspace.get(project_id)
        try:
            return resolve_replace_rules_preview(
                project,
                sheet_id=sheet_id,
                input_column=input_column,
                rules=rules,
                unmatched=unmatched,
                test_value=test_value,
            )
        except ReplaceRulesPreviewError as exc:
            raise PreviewRequestError(
                code=exc.code,
                message=exc.message,
                field=exc.field,
                details=exc.details,
            ) from exc

    async def ocr_compare_preview(
        self,
        project_id: str,
        *,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        try:
            return await compare_ocr_preview(project, payload)
        except OcrComparePreviewError as exc:
            raise PreviewRequestError(
                code=exc.code,
                message=exc.message,
                field=exc.field,
                details=exc.details,
            ) from exc

    async def topic_segmentation_compare_scratch(
        self,
        project_id: str,
        *,
        transcript_bytes: bytes,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Compare topic segmentation over uploaded transcript text.

        Resolve the project only to preserve the project-scoped auth/not-found
        contract. The comparison function receives no Project or router and
        therefore has no project write surface or remote-provider path.
        """

        self._workspace.get(project_id)
        try:
            return await compare_topic_segmentation_scratch(
                transcript_bytes,
                payload,
            )
        except TopicSegmentationCompareError as exc:
            raise PreviewRequestError(
                code=exc.code,
                message=exc.message,
                field=exc.field,
                details=exc.details,
            ) from exc

    async def translate_compare_scratch(
        self,
        project_id: str,
        *,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Translate a pasted text sample through several engines for the
        scratch bake-off — no project writes. NO model router is resolved and
        the billable engines (llm/deepl/google_translate) are refused outright
        and pointed at map.translate; only the local engines run here."""

        project = self._workspace.get(project_id)
        try:
            return await compare_translate_scratch(project, payload)
        except TranslateComparePreviewError as exc:
            raise PreviewRequestError(
                code=exc.code,
                message=exc.message,
                field=exc.field,
                details=exc.details,
            ) from exc
