"""Invocation-owned translation admission and accounting."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from frisket.actions.translate_types import (
    TranslationOptions,
    TranslateOutput,
)
from frisket.actions.types import Row, RowError
from frisket.execution.attempt import routed_admission_in_scope
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.ops.integrations.translate_common import TranslateEngineError
from frisket.ops.integrations.translation_engine import TranslationEngine


class AdmittedTranslator:
    """One provider translation per captured row, with host-owned facts."""

    def __init__(self, ctx: OpContext, *, engine: str, options: dict) -> None:
        self._ctx = ctx
        self._engine = engine
        self._options = TranslationOptions.model_validate(options).normalize(engine)
        if engine == "llm":
            raise ValueError("LLM translation uses the inspected model request")
        self._closed = False
        self._called_rows: set[int] = set()
        self.accounting_by_row: dict[int, dict[str, Any]] = {}
        self.calls_by_row: dict[int, list[dict[str, Any]]] = {}

    async def aclose(self) -> None:
        self._closed = True

    def _check_active(self, ctx: OpContext) -> None:
        if self._closed:
            raise RuntimeError("translator invocation is closed")
        cancelled = ctx.extras.get("cancelled")
        if callable(cancelled) and cancelled():
            raise asyncio.CancelledError

    def _check_route(self, ctx: OpContext) -> None:
        from frisket.execution.resolve_for_action import authored_options
        from frisket.execution.resolver import preview_resolution_in_scope

        admission = routed_admission_in_scope(ctx.extras)
        preview = preview_resolution_in_scope(ctx.extras)
        transports = {
            "deepl": "deepl.v2",
            "google_translate": "google.translate.v2",
            "opus_mt": "local",
            "hy_mt2": "local",
        }
        if admission is not None:
            route = admission.route
            if (
                route.engine != self._engine
                or route.target_snapshot.get("capability") != "translate"
                or route.target_snapshot.get("transport") != transports[self._engine]
                or route.options != authored_options(self._options, "translate")
            ):
                raise RecipeInvocationHalt(
                    "promise_violation",
                    "Translator requires its admitted engine route.",
                )
        elif preview is not None:
            if (
                preview.facts.engine != self._engine
                or preview.support.capability != "translate"
            ):
                raise RecipeInvocationHalt(
                    "promise_violation",
                    "Translator requires its resolved preview engine.",
                )
        else:
            raise RecipeInvocationHalt(
                "promise_violation",
                "Translator requires an admitted translation route.",
            )

    def bind_row(self, row: Row, *, sheet_id, row_id, sources, ctx):
        self._check_active(ctx)
        if type(sheet_id) is not int or type(row_id) is not int or row_id <= 0:
            raise RowError("invalid_input_ref", "Translator requires its admitted row.")
        return _BoundTranslator(self, row, row_id, ctx)


class _BoundTranslator:
    def __init__(
        self, owner: AdmittedTranslator, row: Row, row_id: int, ctx: OpContext
    ):
        self._owner, self._row, self._row_id, self._ctx = owner, row, row_id, ctx

    async def translate(
        self, row: Row, text: str, *, options: TranslationOptions
    ) -> TranslateOutput:
        owner, ctx = self._owner, self._ctx
        owner._check_active(ctx)
        if row is not self._row:
            raise RowError("invalid_input_ref", "Translator requires its admitted row.")
        if not isinstance(text, str) or type(options) is not TranslationOptions:
            raise TypeError("Translator requires text and TranslationOptions")
        if options.normalize(owner._engine) != owner._options:
            raise RowError(
                "invalid_params", "Translation options differ from admission."
            )
        owner._check_route(ctx)
        if self._row_id in owner._called_rows:
            raise RowError("invalid_input_ref", "Translator permits one call per row.")
        owner._called_rows.add(self._row_id)
        artifacts_before = len(ctx.extras.get("artifact_provenance", []))
        try:
            result = await TranslationEngine().execute(
                {"text": text}, {**owner._options, "engine": owner._engine}, ctx
            )
        except TranslateEngineError as error:
            if error.accounting:
                owner.accounting_by_row[self._row_id] = copy.deepcopy(error.accounting)
            raise
        if isinstance(result, tuple):
            data, accounting = result
        else:
            data, accounting = result, {"cost": 0.0, "model_calls": []}
        owner.accounting_by_row[self._row_id] = copy.deepcopy(accounting)
        owner.calls_by_row[self._row_id] = [
            {
                "kind": "translate_read",
                "engine": owner._engine,
                "target_language": owner._options["target_language"],
                "language": copy.deepcopy(owner._options["language"]),
                "artifacts": copy.deepcopy(
                    ctx.extras.get("artifact_provenance", [])[artifacts_before:]
                ),
            }
        ]
        return TranslateOutput.model_validate(data)
