"""Invocation-owned classifier decisions over the admitted model-server/API route."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from frisket.actions.types import Outcome, RowError
from frisket.ai.external_pricing import (
    CLOUDFLARE_CLEF_INPUT_TOKEN,
    OPENAI_DECISIONS_INPUT_TOKEN,
    external_unit_price_usd,
)
from frisket.ai.models.metadata import ModelCallMeta
from frisket.contracts.clef import clef_questions, clef_values, validate_clef_request
from frisket.contracts.classification import DECISION_ENGINE_IDS
from frisket.contracts.decisions import (
    DECISIONS_MODEL,
    DECISIONS_URL,
    decisions_questions,
    decisions_values,
    decisions_usage,
)
from frisket.credentials import ResolvedCredential, normalize_resolved_credential
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.credential_use import (
    CredentialUseRefusal,
    require_consented_credential,
)
from frisket.execution.resolver import preview_resolution_in_scope
from frisket.execution.runtime_binding import bind_fact_to_route
from frisket.ops._sidecar import SidecarHTTPError, _cancellable_wait, sidecar_post
from frisket.ops.base import RecipeInvocationHalt


class AdmittedDecisionClassifier:
    def __init__(self, *, engine: str, context: str = "", cancelled=None):
        if engine not in DECISION_ENGINE_IDS:
            raise ValueError("unsupported decision engine")
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
        return _BoundDecisionClassifier(self, row, row_id, ctx)

    async def aclose(self):
        self._closed = True


class _BoundDecisionClassifier:
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
        openai = owner.engine == "openai-decisions"
        questions = (
            decisions_questions(field_specs, owner.context)
            if openai
            else clef_questions(field_specs, owner.context)
        )
        try:
            validate_clef_request(owner.engine, questions, text=text)
        except ValueError as error:
            raise RowError("classify_input_invalid", str(error)) from None
        admission = routed_admission_in_scope(ctx.extras)
        preview = preview_resolution_in_scope(ctx.extras)
        transport = (
            "openai.decisions"
            if openai
            else "cloudflare.clef"
            if owner.engine == "clef"
            else "sidecar.classify"
        )
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
        if (not openai and not connection.base_url) or not connection.token:
            raise RecipeInvocationHalt(
                "no_live_target", "Classifier connection is not configured"
            )
        source = normalize_resolved_credential(
            ResolvedCredential(
                connection.token, connection.extra.get("credential_source", "local")
            ),
            context=ctx.credential_use_context,
        ).source
        if owner.engine != "clef-flash":
            try:
                require_consented_credential(
                    cost_posture=posture,
                    selected_source=source,
                    context=ctx.credential_use_context,
                    effect=f"the {owner.engine} classification call",
                )
            except CredentialUseRefusal as error:
                raise RecipeInvocationHalt("promise_violation", str(error)) from error
        owner._called_rows.add(self.row_id)

        def record(tokens=None, usage=None, rejected_status=None):
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
                rate = external_unit_price_usd(
                    OPENAI_DECISIONS_INPUT_TOKEN
                    if openai
                    else CLOUDFLARE_CLEF_INPUT_TOKEN
                )
                if openai:
                    usage_units, cost = decisions_usage(usage, rate)
                    units.update(usage_units)
                else:
                    cost = None if tokens is None or rate is None else tokens * rate
                if rejected_status is not None:
                    cost = 0.0
                    units["http_status"] = rejected_status
                fact = ModelCallMeta.provider_call(
                    capability="classify",
                    engine=owner.engine,
                    provider="openai" if openai else "cloudflare",
                    provider_kind="platform_api",
                    model_ids=[f"openai/{DECISIONS_MODEL}"]
                    if openai
                    else ["Cloudflare/clef"],
                    credential_source=source,
                    provider_reported_cost_usd=None,
                    provider_cost_usd=cost,
                    cost_source="provider_rejected"
                    if rejected_status is not None
                    else "unknown"
                    if cost is None
                    else "configured_catalog",
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
                        DECISIONS_URL if openai else connection.base_url,
                        headers={"Authorization": f"Bearer {connection.token}"},
                        json={
                            "model": DECISIONS_MODEL,
                            "input": text,
                            "questions": questions,
                        }
                        if openai
                        else {"model": "clef", "state": text, "questions": questions},
                        timeout=httpx.Timeout(120, connect=10),
                        follow_redirects=False,
                    ),
                    owner.cancelled,
                )
                if response.status_code != 200:
                    # Categorical request rejection did not buy an inference.
                    # 408/5xx and transport failures can be ambiguous: keep unknown.
                    if (
                        openai
                        and 400 <= response.status_code < 500
                        and response.status_code != 408
                    ):
                        record(rejected_status=response.status_code)
                    raise RowError(
                        "classify_request_failed",
                        f"{owner.engine} returned HTTP {response.status_code}",
                    )
                envelope = response.json()
                if openai:
                    body = envelope
                else:
                    if (
                        not isinstance(envelope, dict)
                        or envelope.get("success") is not True
                    ):
                        raise ValueError("invalid Cloudflare response")
                    body = envelope.get("result")
            # Received usage survives late cancellation and output validation.
            usage = body.get("usage") if isinstance(body, dict) else None
            tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
            if type(tokens) is int and tokens >= 0:
                record(tokens, usage=usage)
            owner._check_active()
            if not isinstance(body, dict) or body.get("model") != (
                DECISIONS_MODEL if openai else owner.engine
            ):
                raise ValueError("unexpected decision model")
            values = (decisions_values if openai else clef_values)(
                body.get("answers"), field_specs
            )
        except RowError:
            raise
        except SidecarHTTPError as error:
            if error.status_code == 422:
                raise RowError(
                    "classify_input_invalid",
                    "Clef Flash rejected the input or question schema, which may "
                    "exceed its combined model context limit. Shorten the input, "
                    "context, or instructions, or reduce the fields and labels.",
                ) from None
            raise RowError(
                "classify_request_failed",
                "Could not complete the Clef Flash request; check the model server configuration",
            ) from None
        except (ValueError, TypeError, KeyError):
            raise RowError(
                "classify_output_invalid",
                f"{owner.engine} returned an invalid decision response",
            ) from None
        except (httpx.HTTPError, RuntimeError):
            raise RowError(
                "classify_request_failed",
                f"Could not complete the {owner.engine} request; check its provider or model server configuration",
            ) from None
        return {
            name: Outcome.failed(
                "classify_refused", "The model declined to answer this field."
            )
            if result is None
            else Outcome.ok(result[0], confidence=result[1])
            for name, result in values.items()
        }
