"""Admitted OpenCorporates HTTP and concrete, writer-fenced response facts."""

import asyncio
import json
import uuid
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from frisket.actions.types import RowError
from frisket.engine.jobs.rate_limiter import RateLimitPolicy
from frisket.engine.jobs.rate_limiter import (
    RateLimitCancelled,
    SqliteRateLimiter,
    sqlite_rate_limit_db,
)

CAPABILITY = "external:opencorporates"
TOKEN_NAME = "OPENCORPORATES_API_TOKEN"
RATE_POLICY = RateLimitPolicy(
    max_concurrency=1,
    min_interval_ms=1000,
    burst=1,
    retry_after_cap_ms=30000,
    max_attempts=2,
)


class _Reconcile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    company_name: str = Field(min_length=1)
    jurisdiction_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{2,16}$")


class _Fetch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    jurisdiction_code: str = Field(pattern=r"^[a-z0-9_]{2,16}$")
    company_number: str = Field(min_length=1)


class _Call(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["opencorporates"]
    method: Literal["reconcile", "fetch"]
    arguments: dict


def rate_key(binding):
    # Preserve the legacy per-action key, including its exact JSON spelling.
    return "plugin-rate:" + json.dumps(
        (binding.plugin, binding.kind, "opencorporates.com", TOKEN_NAME),
        separators=(",", ":"),
        sort_keys=True,
    )


class AdmittedOpenCorporates:
    def __init__(self, ctx, binding):
        self.ctx, self.binding = ctx, binding
        self.retry_after = None
        self.response_count = 0
        self.closed = False
        self.client = None

    def check_active(self, ctx=None):
        ctx = ctx or self.ctx
        if self.closed:
            raise RuntimeError("OpenCorporates invocation is closed")
        if ctx.extras.get("preview"):
            raise RowError(
                "preview_unsupported", "OpenCorporates requires a confirmed run."
            )
        if ctx.project.effective_network_policy() == "off":
            raise RowError(
                "network_disabled", "OpenCorporates requires network access."
            )
        cancelled = ctx.extras.get("cancelled")
        if callable(cancelled) and cancelled():
            raise asyncio.CancelledError
        from frisket.execution.attempt import attempt_in_scope

        if attempt_in_scope(ctx.extras) is None:
            raise RuntimeError("OpenCorporates requires its admitted writer")

    async def __aenter__(self):
        return self

    def _ensure_started(self, ctx):
        self.check_active(ctx)
        if self.client is not None:
            return
        from frisket.authoring.workbench.plugin_subprocess import _project_plugin_env

        env = _project_plugin_env(
            ctx.project,
            plugin_id=self.binding.plugin,
            metadata={"requires_secrets": [TOKEN_NAME]},
        )
        if TOKEN_NAME not in env:
            raise RowError(
                "plugin_env_missing",
                "Configure OPENCORPORATES_API_TOKEN before running.",
            )
        self.client = httpx.AsyncClient(
            headers={"X-API-TOKEN": env[TOKEN_NAME], "Accept": "application/json"},
            timeout=20,
            follow_redirects=False,
        )

    async def __aexit__(self, *_):
        self.closed = True
        if self.client is not None:
            await self.client.aclose()

    def record(self, response, method, *, ctx=None):
        ctx = ctx or self.ctx
        from frisket.engine.store.receipts import ReceiptStore
        from frisket.execution.attempt import attempt_in_scope

        writer = attempt_in_scope(ctx.extras)
        ReceiptStore(ctx.project)._record_writer_evidence(
            {
                "kind": "opencorporates_request",
                "call_id": uuid.uuid4().hex,
                "row_id": ctx.extras["row_id"],
                "provider": "opencorporates.com",
                "service": method,
                "status_code": response.status_code,
                "external_api": True,
                "cost_actual": None,
                "cost_source": "external_metered_declared",
            },
            run_id=ctx.extras["run_id"],
            writer_attempt_id=writer.attempt_id,
            claim_token=ctx.extras["claim_token"],
            provider_use_fact={
                "provider": "opencorporates.com",
                "plugin_id": self.binding.plugin,
                "action_kind": self.binding.kind,
                "external_api": True,
                "cost_source": "external_metered_declared",
                "cost_actual": None,
                "effect_count": 1,
                "effect_count_source": "host_http_response",
                "affected_row_count": 1,
            },
        )
        self.response_count += 1

    async def __call__(self, frame, *, ctx=None):
        ctx = ctx or self.ctx
        self._ensure_started(ctx)
        try:
            call = _Call.model_validate(frame)
            if call.method == "reconcile":
                arguments = _Reconcile.model_validate(call.arguments)
                query = {"query": arguments.company_name, "limit": 1}
                if arguments.jurisdiction_code is not None:
                    query["jurisdiction_code"] = arguments.jurisdiction_code
                response = await self.client.post(
                    "https://opencorporates.com/reconcile",
                    data={"queries": json.dumps({"row": query}, separators=(",", ":"))},
                )
            else:
                arguments = _Fetch.model_validate(call.arguments)
                if any(ord(c) < 32 for c in arguments.company_number):
                    raise ValueError("invalid company number")
                response = await self.client.get(
                    "https://api.opencorporates.com/v0.4/companies/"
                    + quote(arguments.jurisdiction_code, safe="")
                    + "/"
                    + quote(arguments.company_number, safe="")
                )
            # Record a concrete response before every status/JSON/child check.
            self.record(response, call.method, ctx=ctx)
            if response.status_code == 429:
                from frisket.plugins.subprocess_runner import _retry_after_ms

                self.retry_after = _retry_after_ms(response.headers.get("Retry-After"))
                if self.retry_after is None:
                    self.retry_after = 0
                return {
                    "type": "opencorporates_result",
                    "error": {
                        "code": "rate_limited",
                        "message": "OpenCorporates rate limit reached.",
                    },
                }
            if call.method == "fetch" and response.status_code == 404:
                value = None
            else:
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("invalid provider response")
                if call.method == "reconcile":
                    row = payload.get("row", payload)
                    value = row.get("result") if isinstance(row, dict) else None
                    if not isinstance(value, list) or not all(
                        isinstance(v, dict) for v in value
                    ):
                        raise ValueError("invalid provider candidates")
                else:
                    results = payload.get("results")
                    value = (
                        results.get("company") if isinstance(results, dict) else None
                    )
                    if not isinstance(value, dict) or (
                        isinstance(value.get("company_number"), bool)
                        or str(value.get("jurisdiction_code", "")).strip().lower()
                        != arguments.jurisdiction_code
                        or str(value.get("company_number", "")).strip()
                        != arguments.company_number
                    ):
                        raise ValueError("provider company key mismatch")
            return {"type": "opencorporates_result", "value": value}
        except (ValueError, httpx.HTTPError):
            return {
                "type": "opencorporates_result",
                "error": {
                    "code": "opencorporates_failed",
                    "message": "OpenCorporates request failed.",
                },
            }

    def bind_row(self, ctx):
        return _BoundOpenCorporates(self, ctx)


class _BoundOpenCorporates:
    def __init__(self, provider: AdmittedOpenCorporates, ctx):
        self._provider = provider
        self._ctx = ctx

    async def _call(self, method, arguments):
        limiter = SqliteRateLimiter(sqlite_rate_limit_db(self._ctx.project.path.parent))
        key = rate_key(self._provider.binding)
        cancelled = self._ctx.extras.get("cancelled")
        for attempt in range(1, RATE_POLICY.max_attempts + 1):
            try:
                limiter.acquire(key, RATE_POLICY, should_cancel=cancelled)
            except RateLimitCancelled:
                raise asyncio.CancelledError from None
            try:
                self._provider.retry_after = None
                response = await self._provider(
                    {
                        "type": "opencorporates",
                        "method": method,
                        "arguments": arguments,
                    },
                    ctx=self._ctx,
                )
                error = response.get("error")
                if (
                    error is None
                    or error.get("code") != "rate_limited"
                    or self._provider.retry_after is None
                    or attempt == RATE_POLICY.max_attempts
                ):
                    if error is not None:
                        raise RowError(error["code"], error["message"])
                    return response["value"]
                limiter.record_backoff(
                    key,
                    retry_after_ms=self._provider.retry_after,
                    policy=RATE_POLICY,
                )
            finally:
                limiter.release(key)
        raise RuntimeError("OpenCorporates retry did not settle")

    async def reconcile(self, company_name, *, jurisdiction_code=None):
        return await self._call(
            "reconcile",
            {"company_name": company_name, "jurisdiction_code": jurisdiction_code},
        )

    async def fetch(self, jurisdiction_code, company_number):
        return await self._call(
            "fetch",
            {
                "jurisdiction_code": jurisdiction_code,
                "company_number": company_number,
            },
        )
