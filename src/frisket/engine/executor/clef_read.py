"""Invocation-owned Clef decisions over the admitted model-server/API route."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from frisket.actions.types import Outcome, RowError
from frisket.ai.external_pricing import (
    CLOUDFLARE_CLEF_INPUT_TOKEN,
    external_unit_price_usd,
)
from frisket.ai.models.metadata import ModelCallMeta
from frisket.contracts.clef import clef_questions, clef_values
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.credential_use import (
    CredentialUseRefusal,
    require_consented_credential,
)
from frisket.execution.resolver import preview_resolution_in_scope
from frisket.execution.runtime_binding import bind_fact_to_route
from frisket.ops._sidecar import _cancellable_wait, sidecar_post
from frisket.ops.base import RecipeInvocationHalt


class AdmittedClefClassifier:
    def __init__(self, *, engine: str, context: str = "", cancelled=None):
        if engine not in {"clef", "clef-flash"}:
            raise ValueError("unsupported Clef engine")
        self.engine, self.context, self.cancelled = engine, context, cancelled
        self._closed = False
        self._called_rows: set[int] = set()
        self.accounting_by_row: dict[int, dict[str, Any]] = {}

    def _check_active(self):
        if self._closed:
            raise RuntimeError("classifier invocation is closed")
        if callable(self.cancelled) and self.cancelled():
            raise asyncio.CancelledError

    def bind_row(self, row, *, row_id, ctx, **_admission):
        self._check_active()
        if type(row_id) is not int or row_id <= 0:
            raise RowError("invalid_input_ref", "Classifier requires its admitted row")
        return _BoundClefClassifier(self, row, row_id, ctx)

    async def aclose(self):
        self._closed = True


class _BoundClefClassifier:
    def __init__(self, owner, row, row_id, ctx):
        self.owner, self.row, self.row_id, self.ctx = owner, row, row_id, ctx

    async def classify(self, row, text, fields):
        owner, ctx = self.owner, self.ctx
        owner._check_active()
        if row is not self.row or self.row_id in owner._called_rows:
            raise RowError(
                "invalid_input_ref", "Classifier permits one call per admitted row"
            )
        if not text.strip():
            raise RowError("classify_input_empty", "Classifier input must contain text")
        field_specs = [field.model_dump() for field in fields]
        questions = clef_questions(field_specs, owner.context)
        admission = routed_admission_in_scope(ctx.extras)
        preview = preview_resolution_in_scope(ctx.extras)
        transport = "cloudflare.clef" if owner.engine == "clef" else "sidecar.classify"
        if admission is not None:
            route = admission.route
            valid = (
                route.engine == owner.engine
                and route.target_snapshot.get("capability") == "classify"
                and route.target_snapshot.get("transport") == transport
            )
            connection, posture = admission.binding.connection, route.cost_posture
        elif preview is not None:
            valid = (
                preview.facts.engine == owner.engine
                and preview.support.capability == "classify"
                and preview.support.transport == transport
            )
            connection, posture = preview.connection, preview.facts.cost_posture
        else:
            valid = False
        if not valid:
            raise RecipeInvocationHalt(
                "promise_violation", "Classifier requires its admitted engine route"
            )
        if not connection.base_url or not connection.token:
            raise RecipeInvocationHalt(
                "no_live_target", "Classifier connection is not configured"
            )
        source = connection.extra.get("credential_source", "local")
        if owner.engine == "clef":
            try:
                require_consented_credential(
                    cost_posture=posture,
                    selected_source=source,
                    context=ctx.credential_use_context,
                    effect="the Cloudflare Clef classification call",
                )
            except CredentialUseRefusal as error:
                raise RecipeInvocationHalt("promise_violation", str(error)) from error
        owner._called_rows.add(self.row_id)

        def record(tokens=None):
            units = {"requests": 1}
            if tokens is not None:
                units["input_tokens"] = tokens
            if owner.engine == "clef-flash":
                cost = 0.0
                fact = ModelCallMeta.sidecar(
                    capability="classify",
                    engine=owner.engine,
                    model_ids=["Cloudflare/clef-flash"],
                    units=units,
                ).as_dict()
            else:
                rate = external_unit_price_usd(CLOUDFLARE_CLEF_INPUT_TOKEN)
                cost = None if tokens is None or rate is None else tokens * rate
                fact = ModelCallMeta.provider_call(
                    capability="classify",
                    engine=owner.engine,
                    provider="cloudflare",
                    provider_kind="platform_api",
                    model_ids=["Cloudflare/clef"],
                    credential_source=source,
                    provider_reported_cost_usd=None,
                    provider_cost_usd=cost,
                    cost_source="unknown" if cost is None else "configured_catalog",
                    units=units,
                    duration_ms=None,
                ).as_dict()
            if admission is not None:
                fact = bind_fact_to_route(admission.route, fact)
            owner.accounting_by_row[self.row_id] = {
                "cost": cost,
                "cost_source": fact["cost_source"],
                "model_calls": [fact],
            }

        # A failed HTTP wait may still have reached the paid provider. Never
        # turn missing usage into a successful zero-dollar provider call.
        record()
        try:
            if owner.engine == "clef-flash":
                body = await sidecar_post(
                    ctx,
                    "/classify",
                    connection=connection,
                    op="classify",
                    json={"engine": owner.engine, "text": text, "questions": questions},
                    should_cancel=owner.cancelled,
                )
            else:
                response = await _cancellable_wait(
                    ctx.http.post(
                        connection.base_url,
                        headers={"Authorization": f"Bearer {connection.token}"},
                        json={"model": "clef", "state": text, "questions": questions},
                        timeout=httpx.Timeout(120, connect=10),
                        follow_redirects=False,
                    ),
                    owner.cancelled,
                )
                if response.status_code != 200:
                    raise RowError(
                        "classify_request_failed",
                        f"Cloudflare Clef returned HTTP {response.status_code}",
                    )
                envelope = response.json()
                if (
                    not isinstance(envelope, dict)
                    or envelope.get("success") is not True
                ):
                    raise ValueError("invalid Cloudflare response")
                body = envelope.get("result")
            owner._check_active()
            if not isinstance(body, dict) or body.get("model") != owner.engine:
                raise ValueError("unexpected Clef model")
            usage = body.get("usage")
            tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
            if type(tokens) is int and tokens >= 0:
                record(tokens)
            values = clef_values(body.get("answers"), field_specs)
        except RowError:
            raise
        except (ValueError, TypeError, KeyError):
            raise RowError(
                "classify_output_invalid", "Clef returned an invalid decision response"
            ) from None
        except (httpx.HTTPError, RuntimeError):
            raise RowError(
                "classify_request_failed",
                "Could not complete the Clef request; check the model server or Cloudflare configuration",
            ) from None
        return {
            name: Outcome.ok(value, confidence=confidence)
            for name, (value, confidence) in values.items()
        }
