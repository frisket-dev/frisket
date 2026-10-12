"""Shared Project Ask inventory eligibility for selector and admission."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def project_ask_inventory(
    provider_catalog: Mapping[str, Any],
    *,
    router: Any,
    project: Any,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, Any]]:
    """Return eligible models, direct-local models, and cached spend states."""

    configured_providers = frozenset(router.providers())
    candidates: list[str] = []
    direct: list[str] = []
    spend_by_provider: dict[str, Any] = {}
    for provider in provider_catalog.get("providers", []):
        if not isinstance(provider, Mapping):
            continue
        local_http = provider.get("kind") == "local_http"
        raw_provider_id = (
            provider.get("endpoint_id") if local_http else provider.get("id")
        )
        if not isinstance(raw_provider_id, str) or not raw_provider_id:
            continue
        local_ready = local_http and bool(provider.get("reachable"))
        if local_http and not local_ready:
            continue
        spend = None
        if raw_provider_id in configured_providers:
            source = provider.get("credential_source")
            if not isinstance(source, str) or not source:
                source = router.credential_source_for(raw_provider_id)
            if source == "project_key":
                spend = project.provider_spend_state(raw_provider_id)
                spend_by_provider[raw_provider_id] = spend
                if spend is not None and (spend.over_cap or not spend.cap_enforceable):
                    continue
        for model in provider.get("models", []):
            model_id = model.get("id") if isinstance(model, Mapping) else None
            if not isinstance(model_id, str) or not model_id:
                continue
            candidates.append(model_id)
            if local_ready:
                direct.append(model_id)
    return (
        tuple(dict.fromkeys(candidates)),
        tuple(dict.fromkeys(direct)),
        spend_by_provider,
    )
