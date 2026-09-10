"""Invocation-owned geocoder using the existing admitted provider route."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from frisket.actions.geospatial_types import GeocodedAddress
from frisket.actions.types import GeoPoint, Outcome
from frisket.ai.external_pricing import (
    GEOCODE_EXTERNAL_GEOCODER,
    GEOCODE_NOMINATIM_ROW,
    estimate_external_cost,
)
from frisket.ai.models.metadata import ModelCallMeta, model_calls_cost_actual
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.credential_use import (
    CredentialUseRefusal,
    require_consented_credential,
)
from frisket.execution.runtime_binding import ROUTE_OBSERVATION_KEY, bind_fact_to_route
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.ops.cost_source import free_public_api_estimate
from frisket.ops.integrations.hosted_error import HostedEngineError
from frisket.redaction import redact_text

OPENCAGE_URL = "https://api.opencagedata.com/geocode/v1/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Nominatim usage policy: max 1 request/second, identified User-Agent.
NOMINATIM_MIN_INTERVAL = 1.1
_nominatim_last = 0.0
_nominatim_locks: dict[int, asyncio.Lock] = {}  # one lock per event loop


class GeocodeEngineError(HostedEngineError, RuntimeError):
    """Hosted geocoder failure that keeps the recipe's RuntimeError API."""


def _user_agent() -> str:
    try:
        from importlib.metadata import version

        from frisket import DISTRIBUTION_NAME

        v = version(DISTRIBUTION_NAME)
    except Exception:  # noqa: BLE001 — uninstalled dev tree
        v = "dev"
    return f"frisket/{v} (https://frisket.dev)"


async def _nominatim_pace(
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Serialize Nominatim calls to >= NOMINATIM_MIN_INTERVAL apart, across
    the runner's parallel rows (OSM policy: absolute max one req/second).

    ``clock``/``sleep`` are injected time seams: plain callables
    defaulting to real time so tests pin the pacing arithmetic on a manual
    clock; production callers stay unchanged."""
    global _nominatim_last
    loop_key = id(asyncio.get_running_loop())
    lock = _nominatim_locks.setdefault(loop_key, asyncio.Lock())
    async with lock:
        wait = NOMINATIM_MIN_INTERVAL - (clock() - _nominatim_last)
        if wait > 0:
            await sleep(wait)
        _nominatim_last = clock()


class AdmittedGeocoder:
    """One logical lookup per admitted row; internal retries retain their facts."""

    def __init__(self, ctx: OpContext) -> None:
        self._ctx = ctx
        self._closed = False
        self._called_rows: set[int] = set()
        self.accounting_by_row: dict[int, dict[str, Any]] = {}
        self.request_count = 0

    def bind_row(self, ctx: OpContext) -> _GeocoderRow:
        return _GeocoderRow(self, ctx)

    async def aclose(self) -> None:
        self._closed = True

    def _check_active(self) -> None:
        if self._closed:
            raise RuntimeError("geocoder invocation is closed")
        cancelled = self._ctx.extras.get("cancelled")
        if cancelled and cancelled():
            raise asyncio.CancelledError()

    async def _lookup(self, ctx: OpContext, address: str) -> GeocodedAddress:
        self._check_active()
        row_id = ctx.extras.get("row_id")
        if not isinstance(row_id, int) or row_id in self._called_rows:
            raise RuntimeError("geocoder permits one lookup per admitted row")
        self._called_rows.add(row_id)
        if not isinstance(address, str):
            raise TypeError("geocoder address must be text")
        address = address.strip()
        if not address:
            self.accounting_by_row[row_id] = {"cost": 0.0, "model_calls": []}
            return GeocodedAddress(geo_point=Outcome.ok(None), formatted_address=None)
        admission = routed_admission_in_scope(ctx.extras)
        if admission is None:
            raise RuntimeError("geocoder requires an admitted geocode route")
        engine = admission.route.engine
        if engine not in {"opencage", "nominatim"}:
            raise RuntimeError("admitted geocoder engine is not supported")
        snapshot = admission.route.target_snapshot
        expected_transport = (
            "opencage.v1" if engine == "opencage" else "nominatim.search"
        )
        if (
            snapshot.get("capability") != "geocode"
            or snapshot.get("transport") != expected_transport
        ):
            raise RecipeInvocationHalt(
                "promise_violation", "Geocoder requires its admitted provider transport"
            )
        if ctx.http is None:
            raise RuntimeError("geocoder requires an HTTP client")
        try:
            if engine == "opencage":
                from frisket.credentials import resolve_credential_for_use

                credential = resolve_credential_for_use(
                    ctx.project, "OPENCAGE_API_KEY", context=ctx.credential_use_context
                )
                if credential is None:
                    raise RecipeInvocationHalt(
                        "promise_violation",
                        "OpenCage credential is unavailable; review and re-consent",
                    )
                try:
                    require_consented_credential(
                        cost_posture=admission.route.cost_posture,
                        selected_source=credential.source,
                        selected_owner=credential.owner,
                        context=ctx.credential_use_context,
                        effect="the hosted OpenCage geocoding call",
                    )
                except CredentialUseRefusal as exc:
                    raise RecipeInvocationHalt(
                        "promise_violation",
                        f"{exc}; review the claims and re-consent to resume",
                    ) from exc
                hit, accounting = await self._opencage(
                    address,
                    ctx,
                    credential.value,
                    credential_source=credential.source,
                    pricing_key=GEOCODE_EXTERNAL_GEOCODER,
                )
            else:
                hit, accounting = await self._nominatim(
                    address, ctx, pricing_key=GEOCODE_NOMINATIM_ROW
                )
        except HostedEngineError as exc:
            if exc.accounting:
                self.accounting_by_row[row_id] = exc.accounting
            raise
        self.accounting_by_row[row_id] = accounting
        if hit is None:
            return GeocodedAddress(
                geo_point=Outcome.ok(
                    None,
                    confidence=0.0,
                    justification=f"no result from {engine} for {address!r}",
                ),
                formatted_address=None,
            )
        return GeocodedAddress(
            geo_point=Outcome.ok(
                GeoPoint(lat=hit["lat"], lon=hit["lon"]),
                confidence=hit.get("confidence"),
                justification=f"geocoded by {engine}",
            ),
            formatted_address=hit["formatted"],
        )

    @staticmethod
    def _provider_accounting(
        *,
        engine: str,
        pricing_key: str,
        credential_source: str,
        attempts: list[tuple[int, str | None]],
        final_response_succeeded: bool,
        route: Any | None = None,
    ) -> dict[str, Any]:
        """``route`` is the admitted route row, or ``None`` for
        an unrouted call (previews, direct dispatch) — the same optional the
        recipe was always missing before geocode joined the seam. When
        present, every fact this mint produces carries the §6 binding
        observation (``bind_fact_to_route``) BEFORE it leaves this function,
        so ``RunResultStore``'s epoch invariant (geocode is in
        ``_ROUTED_FACT_CAPABILITIES``) never sees a routed run's fact with
        no observation payload."""
        free_public = pricing_key == GEOCODE_NOMINATIM_ROW
        estimate = (
            free_public_api_estimate(quantity=len(attempts))
            if free_public
            else estimate_external_cost(pricing_key, len(attempts))
        )
        per_request = (
            free_public_api_estimate(quantity=1)
            if free_public
            else estimate_external_cost(pricing_key, 1)
        )
        facts: list[dict[str, Any]] = []
        last_attempt = len(attempts) - 1
        for index, (status, request_id) in enumerate(attempts):
            # Invocation proves one request attempt occurred. Only the final parsed
            # success can use the configured tariff as its cost estimate;
            # rejected/retry responses prove egress but not what the provider
            # ultimately charged for it.
            priced = free_public or (
                final_response_succeeded and index == last_attempt and status == 200
            )
            cost = per_request["cost"] if priced else None
            warnings = (
                []
                if priced
                else [
                    f"geocode provider returned HTTP {status}; request cost is unknown"
                    if status
                    else "geocode request returned no response; request cost is unknown"
                ]
            )
            fact = ModelCallMeta.provider_call(
                capability="geocode",
                engine=engine,
                provider=engine,
                provider_kind="platform_api",
                credential_source=credential_source,
                provider_reported_cost_usd=None,
                provider_cost_usd=cost,
                cost_source=(
                    str(per_request.get("cost_source") or "unknown")
                    if priced
                    else "unknown"
                ),
                units={"requests": 1, "rows": 1},
                request_id=request_id,
                warnings=warnings,
                duration_ms=None,
            ).as_dict()
            if route is not None:
                fact = bind_fact_to_route(route, fact)
                if not fact.get(ROUTE_OBSERVATION_KEY):
                    raise RuntimeError(
                        "geocode fact built under a route carries no binding "
                        "observation required by the route-epoch invariant"
                    )
            facts.append(fact)
        actual = model_calls_cost_actual(facts)
        return {
            **estimate,
            "cost": actual,
            "cost_source": (
                str(estimate.get("cost_source") or "unknown")
                if actual is not None
                else "unknown"
            ),
            "model_calls": facts,
        }

    async def _opencage(
        self,
        address: str,
        ctx: OpContext,
        api_key: str,
        *,
        credential_source: str,
        pricing_key: str,
    ) -> tuple[dict | None, dict[str, Any]]:
        data, accounting = await self._get(
            ctx,
            OPENCAGE_URL,
            params={
                "q": address,
                "key": api_key,
                "limit": 1,
                "no_annotations": 1,
            },
            engine="opencage",
            pricing_key=pricing_key,
            credential_source=credential_source,
            secret_values=(api_key,),
        )
        try:
            results = data.get("results") or []
            if not results:
                return None, accounting
            top = results[0]
            geom = top.get("geometry") or {}
            point = GeoPoint(lat=float(geom["lat"]), lon=float(geom["lng"]))
            out = {
                "lat": point.lat,
                "lon": point.lon,
                "formatted": top.get("formatted", ""),
            }
            if out["formatted"] is not None and not isinstance(out["formatted"], str):
                raise ValueError("formatted address must be text")
            # OpenCage confidence is 1 (vague) .. 10 (precise) — map onto the
            # review queue's 0..1 channel so vague matches surface for review
            if isinstance(top.get("confidence"), (int, float)):
                out["confidence"] = round(min(10, max(0, top["confidence"])) / 10, 2)
            return out, accounting
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            raise GeocodeEngineError(
                code="invalid_response",
                message="OpenCage returned an invalid geocoding response",
                accounting=self._unknown_accounting(
                    accounting, "OpenCage response could not be validated"
                ),
            ) from None

    async def _nominatim(
        self, address: str, ctx: OpContext, *, pricing_key: str
    ) -> tuple[dict | None, dict[str, Any]]:
        await _nominatim_pace()
        data, accounting = await self._get(
            ctx,
            NOMINATIM_URL,
            params={"q": address, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": _user_agent()},
            engine="nominatim",
            pricing_key=pricing_key,
            credential_source="none",
        )
        try:
            if not data:
                return None, accounting
            top = data[0]
            point = GeoPoint(lat=float(top["lat"]), lon=float(top["lon"]))
            formatted = top.get("display_name", "")
            if formatted is not None and not isinstance(formatted, str):
                raise ValueError("formatted address must be text")
            return {
                "lat": point.lat,
                "lon": point.lon,
                "formatted": formatted,
            }, accounting
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise GeocodeEngineError(
                code="invalid_response",
                message=f"Nominatim returned an invalid geocoding response: {exc}",
                accounting=self._unknown_accounting(
                    accounting, "Nominatim response could not be validated"
                ),
            ) from exc

    @staticmethod
    def _unknown_accounting(accounting: dict[str, Any], warning: str) -> dict[str, Any]:
        """Downgrade configured estimates when a returned body is unusable."""
        free_public = accounting.get("cost_source") == "free_public_api"
        for fact in accounting.get("model_calls") or []:
            if not isinstance(fact, dict):
                continue
            if not free_public:
                fact["provider_cost_usd"] = None
                fact["cost_source"] = "unknown"
            fact["warnings"] = [*(fact.get("warnings") or []), warning]
        if not free_public:
            accounting["cost"] = None
            accounting["cost_source"] = "unknown"
        return accounting

    async def _get(
        self,
        ctx: OpContext,
        url: str,
        params: dict,
        headers: dict | None = None,
        *,
        engine: str,
        pricing_key: str,
        credential_source: str,
        secret_values: tuple[str, ...] = (),
    ) -> tuple[Any, dict[str, Any]]:
        """GET with polite 429 retries (the ocr-sidecar backoff pattern)."""
        # Read the admitted route once — every accounting mint
        # below binds its facts to it, so an unrouted caller (previews,
        # direct dispatch) sees byte-identical behavior to before geocode
        # joined the seam.
        admission = routed_admission_in_scope(ctx.extras)
        route = admission.route if admission is not None else None
        attempts: list[tuple[int, str | None]] = []
        for attempt in range(4):
            self._check_active()
            self.request_count += 1
            attempts.append((0, None))
            self.accounting_by_row[ctx.extras["row_id"]] = self._provider_accounting(
                engine=engine,
                pricing_key=pricing_key,
                credential_source=credential_source,
                attempts=attempts,
                final_response_succeeded=False,
                route=route,
            )
            try:
                resp = await ctx.http.get(url, params=params, headers=headers)
            except httpx.HTTPError as exc:
                host = urlsplit(url).hostname
                accounting = AdmittedGeocoder._provider_accounting(
                    engine=engine,
                    pricing_key=pricing_key,
                    credential_source=credential_source,
                    attempts=attempts,
                    final_response_succeeded=False,
                    route=route,
                )
                raise GeocodeEngineError(
                    code="transport",
                    message=f"geocoding request could not reach {host}: "
                    + redact_text(str(exc), secret_values=secret_values)[:300],
                    retryable=True,
                    accounting=accounting,
                ) from None
            request_id = resp.headers.get("x-request-id") or resp.headers.get(
                "request-id"
            )
            if request_id is not None:
                request_id = redact_text(request_id, secret_values=secret_values)
            attempts[-1] = (resp.status_code, request_id)
            # Keep response-proven facts before the next retry await, so a
            # cancelled invocation cannot lose an already returned response.
            self.accounting_by_row[ctx.extras["row_id"]] = self._provider_accounting(
                engine=engine,
                pricing_key=pricing_key,
                credential_source=credential_source,
                attempts=attempts,
                final_response_succeeded=False,
                route=route,
            )
            if resp.status_code != 429:
                break
            await asyncio.sleep(1.5 * (attempt + 1))
        accounting = AdmittedGeocoder._provider_accounting(
            engine=engine,
            pricing_key=pricing_key,
            credential_source=credential_source,
            attempts=attempts,
            final_response_succeeded=resp.status_code == 200,
            route=route,
        )
        if resp.status_code != 200:
            host = urlsplit(url).hostname
            raise GeocodeEngineError(
                code="http",
                message=(
                    f"geocoding request to {host} failed "
                    f"({resp.status_code}): "
                    + redact_text(resp.text, secret_values=secret_values)[:300]
                ),
                retryable=resp.status_code == 429 or resp.status_code >= 500,
                accounting=accounting,
            )
        try:
            return resp.json(), accounting
        except ValueError:
            host = urlsplit(url).hostname
            raise GeocodeEngineError(
                code="invalid_response",
                message=f"geocoding response from {host} was not valid JSON",
                accounting=AdmittedGeocoder._unknown_accounting(
                    accounting, "geocode response body was not valid JSON"
                ),
            ) from None


class _GeocoderRow:
    def __init__(self, owner: AdmittedGeocoder, ctx: OpContext) -> None:
        self._owner = owner
        self._ctx = ctx

    async def lookup(self, address: str) -> GeocodedAddress:
        return await self._owner._lookup(self._ctx, address)
