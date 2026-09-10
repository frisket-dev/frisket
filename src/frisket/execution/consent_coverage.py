"""Request-scoped actor and per-run cost preapproval."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class ConsentCoverage:
    """Trusted consent authority selected for one action request.

    Managed editions bind this value to the authenticated user. Local callers
    omit it and use the installation's persisted local preference.
    """

    principal: str
    threshold_usd: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.principal, str) or not self.principal.strip():
            raise ValueError("consent coverage principal must be a non-empty string")
        if not isinstance(self.threshold_usd, Decimal):
            raise TypeError("consent coverage threshold_usd must be Decimal")
        if not self.threshold_usd.is_finite() or self.threshold_usd < 0:
            raise ValueError(
                "consent coverage threshold_usd must be a non-negative finite decimal"
            )


def effective_consent_coverage(
    project: Any, coverage: ConsentCoverage | None = None
) -> ConsentCoverage:
    if coverage is not None:
        return coverage
    from frisket.engine.store.execution_routes import (
        _installation_root,
        instance_principal,
    )
    from frisket.local_runtime_config import resolve_local_cost_preapproval_usd

    return ConsentCoverage(
        instance_principal(project),
        Decimal(resolve_local_cost_preapproval_usd(_installation_root(project))),
    )


__all__ = ["ConsentCoverage", "effective_consent_coverage"]
