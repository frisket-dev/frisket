"""HTTP contracts for the error-intake seam (client errors + diagnostic bundles).

Edition-additive rule, per the accepted error-intake adjudication: `ok`/`id`
(client errors) and `ok`/`report_id`/`bundle` (diagnostic bundles) are the
cross-edition intersection; `accepted` is a public-Team-only additive field
declared optional so the shared client can never come to require it. The
diagnostic-bundle REQUEST deliberately has no model here — the public route's
contract is a raw dict that is sanitized wholesale, and bounding it would
smuggle in the private edition's refusal semantics. `bundle` contents stay an
open leaf because the two editions compose them differently on purpose.
"""

from __future__ import annotations

from pydantic import JsonValue

from frisket.contracts.http.models import CoerciveRequest, WireModel


class ClientErrorReportRequest(CoerciveRequest):
    """What the browser error hook actually sends; both editions accept it."""

    source: str = "browser"
    severity: str = "error"
    name: str | None = None
    message: str = ""
    stack: str | None = None
    route: str | None = None
    context: dict[str, JsonValue] | None = None


class ClientErrorAccepted(WireModel):
    accepted: bool | None = None
    ok: bool
    id: int


class DiagnosticBundleResult(WireModel):
    ok: bool
    report_id: int
    bundle: dict[str, JsonValue]


__all__ = [
    "ClientErrorAccepted",
    "ClientErrorReportRequest",
    "DiagnosticBundleResult",
]
