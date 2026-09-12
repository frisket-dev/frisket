"""Launcher-safe v1 action catalog hint projection."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from frisket.ops import ytdlp
from frisket.actions.system import root_action_catalog_payload
from frisket.contracts.actions.schemas._engines import (
    CENSUS_ENGINE_TABLE,
    GEOCODE_ENGINE_TABLE,
    OCR_ENGINE_TABLE,
    TO_MARKDOWN_ENGINE_TABLE,
    TRANSCRIBE_ENGINE_TABLE,
    EngineDeclaration,
)
from frisket.credentials import missing_required_credentials
from frisket.ai.external_pricing import (
    DATALAB_CONVERT_PAGE,
    DATALAB_OCR_PAGE,
    GEOCODE_EXTERNAL_GEOCODER,
    external_pricing_entry,
)
from frisket.authoring.plugin_registry import RuntimeBindingSpec, default_registry
from frisket.engine.store import Project
from frisket.execution.commercial import CommercialOffering
from frisket.execution.price_book import PlatformMetered
from frisket.execution.provider import ExecutionComposition
from frisket.execution.targets import ExecutionTarget, TargetEngineSupport


ACTION_PROVIDER_DEFAULT_OUTPUT = {
    "agent": {"name": "answer", "type": "text"},
    "census_demographics": {"name": "demo_population", "type": "number"},
    "extract_faces": {"name": "faces", "type": "json"},
    "geocode": {"name": "geo_point", "type": "geo_point"},
    "ner": {"name": "entities", "type": "json"},
    "video_frames": {"name": "frames", "type": "json"},
}
ACTION_HINT_PROSE_KEYS = {"description", "message", "notes"}
ACTION_CATALOG_LAUNCHER_HINT_COLLISION_ALLOW_SET = frozenset(
    {("map.find_topic_sections", "engines")}
)


def _table_decls(
    table: tuple[EngineDeclaration, ...],
) -> dict[str, EngineDeclaration]:
    """id -> declaration for a family engine table; the catalog derives each
    engine's label (and, for symbolic engines, tier metadata) from the same
    table the contract/ops/executor consume instead of restating copy."""
    return {entry.id: entry for entry in table}


def _sidecar_engine(
    capabilities: dict[str, Any], *, route: str, name: str
) -> tuple[bool, str | None, list[str]]:
    for engine in capabilities.get("engines", []):
        if not isinstance(engine, dict):
            continue
        if engine.get("route") == route and engine.get("name") == name:
            models = engine.get("models", [])
            return (
                bool(engine.get("available")),
                engine.get("error") if isinstance(engine.get("error"), str) else None,
                models if isinstance(models, list) else [],
            )
    if not capabilities.get("configured"):
        return (
            False,
            "Configure FRISKET_MODELS_URL and FRISKET_MODELS_TOKEN "
            "to enable this sidecar engine.",
            [],
        )
    return (
        False,
        capabilities.get("error")
        if isinstance(capabilities.get("error"), str)
        else (f"Sidecar engine '{name}' is not advertised by /capabilities."),
        [],
    )


def _sidecar_v1_engine(
    capabilities: dict[str, Any],
    *,
    route: str,
    name: str,
    contract_version: str,
) -> tuple[bool, str | None, list[str]]:
    """Find a sidecar engine advertising the required worker contract."""
    legacy_match = False
    for engine in capabilities.get("engines", []):
        if not isinstance(engine, dict):
            continue
        if engine.get("route") == route and engine.get("name") == name:
            versions = engine.get("contract_versions")
            if not isinstance(versions, list) or contract_version not in versions:
                legacy_match = True
                continue
            models = engine.get("models", [])
            return (
                bool(engine.get("available")),
                engine.get("error") if isinstance(engine.get("error"), str) else None,
                models if isinstance(models, list) else [],
            )
    if legacy_match:
        return (
            False,
            f"Sidecar advertises '{name}' without contract "
            f"{contract_version}; the gateway worker path is required.",
            [],
        )
    if not capabilities.get("configured"):
        return (
            False,
            "Configure FRISKET_MODELS_URL and FRISKET_MODELS_TOKEN "
            "to enable this sidecar engine.",
            [],
        )
    return (
        False,
        capabilities.get("error")
        if isinstance(capabilities.get("error"), str)
        else (f"Sidecar engine '{name}' is not advertised by /capabilities."),
        [],
    )


def _engine(
    engine_id: str,
    label: str,
    *,
    tier: str,
    billable: bool = False,
    available: bool = True,
    error: str | None = None,
    models: list[str] | None = None,
    pricing: dict[str, Any] | None = None,
    language: dict[str, Any] | None = None,
    diarization: dict[str, Any] | None = None,
    transcription_options: dict[str, bool] | None = None,
    license_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": engine_id,
        "label": label,
        "tier": tier,
        "billable": billable,
        "available": available,
    }
    if error:
        out["error"] = error
    if models:
        out["models"] = models
    if pricing:
        out["pricing"] = pricing
    if language is not None:
        out["language"] = language
    if diarization is not None:
        out["diarization"] = diarization
    if transcription_options is not None:
        out["transcription_options"] = transcription_options
    # Omit permissive licenses; presence signals a restricted/custom license.
    if license_hint is not None:
        out["license"] = license_hint
    return out


def _declared_engine(
    declaration: EngineDeclaration,
    *,
    available: bool = True,
    error: str | None = None,
    models: list[str] | None = None,
    pricing: dict[str, Any] | None = None,
    language: dict[str, Any] | None = None,
    diarization: dict[str, Any] | None = None,
    transcription_options: dict[str, bool] | None = None,
    license_hint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Combine stable contract metadata with live catalog hints."""
    return _engine(
        declaration.id,
        declaration.label,
        tier=declaration.tier,
        billable=declaration.billable,
        available=available,
        error=error,
        models=models,
        pricing=pricing,
        language=language,
        diarization=diarization,
        transcription_options=transcription_options,
        license_hint=license_hint,
    )


def _support_matches_engine(support: TargetEngineSupport, engine: str) -> bool:
    """Whether one target support row serves this exact authorable engine.

    Commercial offers always name the concrete engine.  A provider wildcard is
    only a target declaration, so it may establish capability support here but
    never stand in for an exact offer lookup.
    """

    return support.engine == engine or (
        support.engine.endswith("/*") and engine.startswith(support.engine[:-1])
    )


def _composition_targets_for_engine(
    composition: ExecutionComposition,
    *,
    capability: str,
    engine: str,
) -> list[tuple[ExecutionTarget, TargetEngineSupport]]:
    """The catalog candidates from the composition's resolution target view.

    ``resolution_targets`` is load-bearing: for a platform-metered composition
    it contains no target support without a matching injected offer.  The
    additional exact-offer check below closes the wildcard case, where one
    concrete provider/model offer must not make every model sharing the
    provider wildcard appear in the catalog.
    """

    candidates: list[tuple[ExecutionTarget, TargetEngineSupport]] = []
    for target in composition.resolution_targets():
        support = next(
            (
                row
                for row in target.engines
                if row.capability == capability and _support_matches_engine(row, engine)
            ),
            None,
        )
        if support is None:
            continue
        candidates.append((target, support))
    return candidates


def _catalog_target_family(target_id: str) -> str:
    """Return the target's stable catalog identity."""

    return target_id


def _matching_existing_target_hint(
    hints: list[dict[str, Any]], target_id: str
) -> dict[str, Any] | None:
    family = _catalog_target_family(target_id)
    return next(
        (
            hint
            for hint in hints
            if hint.get("target_id") == target_id or hint.get("target") == family
        ),
        None,
    )


def _composition_target_liveness(
    composition: ExecutionComposition, target_id: str
) -> tuple[bool, str | None]:
    try:
        available = composition.provider.connection(target_id) is not None
    except ValueError as exc:
        return False, str(exc)
    if available:
        return True, None
    hook = getattr(composition.provider, "liveness_remedy", None)
    if callable(hook):
        try:
            remedy = hook(target_id)
        except TypeError:
            remedy = None
        if remedy:
            return False, str(remedy)
    return False, f"Execution target '{target_id}' is not currently available."


def _commercial_pricing_hint(
    *,
    target: ExecutionTarget,
    offering: CommercialOffering,
) -> dict[str, Any]:
    """Project a generic injected offer through the existing pricing envelope.

    This is display metadata only.  The authoritative quote is still minted
    from the immutable offering and pinned into the cost basis at resolution.
    """

    rate = Decimal(offering.quote.unit_rate)
    presentation = offering.presentation
    return {
        "key": offering.quote.pricing_key,
        "label": presentation.billing_label,
        "provider": target.operator,
        "unit": offering.quote.quantity_unit,
        "unit_price_usd": float(rate),
        "unit_price_usd_string": offering.quote.unit_rate,
        "env_var": None,
        "billable": True,
        "external_api": target.egress_class not in {"none", "operator_lan"},
        "cost_source": "pricing_data",
        "description": presentation.venue_label,
        "terms_version": offering.quote.terms_version,
        "charge_authority": offering.charge_authority,
        "venue_label": presentation.venue_label,
        "billing_label": presentation.billing_label,
    }


def _project_execution_composition_engines(
    engines: list[dict[str, Any]],
    *,
    capability: str | None,
    composition: ExecutionComposition | None,
    declared_gateway_fallback: bool = False,
) -> list[dict[str, Any]]:
    """Constrain a project engine roster to its request composition.

    Projectless catalogs deliberately pass no composition and remain neutral.
    For a project, an engine absent from the provider's resolution view is
    absent from the catalog too.  Platform funding contributes no option by
    itself: the exact selected target/capability/engine must carry an offer.
    """

    if composition is None or capability is None:
        return engines

    platform_metered = isinstance(composition.facts.funding, PlatformMetered)
    projected: list[dict[str, Any]] = []
    for original in engines:
        engine_id = original.get("id")
        if not isinstance(engine_id, str):
            continue
        candidates = _composition_targets_for_engine(
            composition,
            capability=capability,
            engine=engine_id,
        )
        # ``llm`` is a pseudo-engine resolved to a concrete provider later.
        if not candidates and not (not platform_metered and engine_id == "llm"):
            continue

        engine = dict(original)
        existing_targets = [
            dict(row) for row in (engine.get("targets") or []) if isinstance(row, dict)
        ]

        def projected_liveness(target_id: str) -> tuple[bool, str | None]:
            available, error = _composition_target_liveness(composition, target_id)
            existing = _matching_existing_target_hint(existing_targets, target_id)
            liveness_hint = existing if existing is not None else original
            if (
                target_id == "models-gateway"
                and not declared_gateway_fallback
                and liveness_hint.get("available") is False
            ):
                engine_error = liveness_hint.get("error")
                return False, engine_error if isinstance(engine_error, str) else error
            return available, error

        target_hints: list[dict[str, Any]] = []
        for target, _support in candidates:
            existing = _matching_existing_target_hint(existing_targets, target.id)
            target_hint = dict(existing or {})
            target_hint.setdefault("target", _catalog_target_family(target.id))
            target_hint["target_id"] = target.id

            offering = composition.offering_for(
                target_id=target.id,
                capability=capability,
                engine=engine_id,
            )
            use_gateway_declaration = (
                declared_gateway_fallback and target.id == "models-gateway"
            )
            if offering is not None or use_gateway_declaration:
                available, error = projected_liveness(target.id)
                target_hint["available"] = available
                if error:
                    target_hint["error"] = error
                else:
                    target_hint.pop("error", None)
            if offering is not None:
                target_hint["billable"] = True
                target_hint["pricing"] = _commercial_pricing_hint(
                    target=target,
                    offering=offering,
                )
            target_hints.append(target_hint)

        if existing_targets or platform_metered:
            engine["targets"] = target_hints

        # Match the resolver's declaration-order choice.
        if candidates:
            preferred_target, _preferred_support = candidates[0]
            preferred_offer = composition.offering_for(
                target_id=preferred_target.id,
                capability=capability,
                engine=engine_id,
            )
            use_gateway_declaration = (
                declared_gateway_fallback and preferred_target.id == "models-gateway"
            )
            if preferred_offer is not None or use_gateway_declaration:
                available, error = projected_liveness(preferred_target.id)
                engine_update = {
                    "available": available,
                    "target_id": preferred_target.id,
                }
                if preferred_offer is not None:
                    engine_update.update(
                        {
                            "tier": "hosted",
                            "billable": True,
                            "pricing": _commercial_pricing_hint(
                                target=preferred_target,
                                offering=preferred_offer,
                            ),
                        }
                    )
                engine.update(engine_update)
                if error:
                    engine["error"] = error
                else:
                    engine.pop("error", None)
        projected.append(engine)
    return projected


def _configured_llm_providers(
    project: Project | None,
    org_provider_keys: Mapping[str, str] | None = None,
    *,
    has_local_model_endpoint: bool = False,
    effective_router: Any = None,
) -> set[str]:
    """LLM chat providers that have a usable key across the SAME sources
    execution resolves: the effective router (including app-injected keys),
    process env, the workspace AI-Providers file
    (``resolve_effective_keys``), per-project keys, AND any team/org-injected
    keys (``Workspace.org_provider_keys()``,
    the SAME ``_provider_keys_resolver()`` call ``router_for`` uses at
    execution time), plus whether an ordinary explicit local endpoint exists.
    Env-only would miss keys configured through the product
    UI (server/workspace.py:router_for). The workspace root is the project
    bundle's parent in the local layout (``<root>/<pid>.frisket``); a
    non-flat edition degrades gracefully to env + project keys via the
    guards. ``org_provider_keys`` is caller-supplied (threaded from the
    route, server/routes/actions.py) rather than resolved here, since only
    the route has the ``Workspace`` in scope — this function stays
    ``Project``-only otherwise, matching every other call site (tests,
    launcher/global catalog) that has no workspace at all."""
    from frisket.server import provider_config

    keyed = set(provider_config.ENV_VAR)
    providers: set[str] = {
        provider
        for provider, env_name in provider_config.ENV_VAR.items()
        if os.environ.get(env_name)
    }
    if effective_router is not None:
        try:
            providers.update(
                provider
                for provider in effective_router.providers()
                if provider in keyed
            )
        except Exception:
            pass
    if org_provider_keys:
        providers.update(k for k in org_provider_keys if k in keyed)
    if has_local_model_endpoint:
        providers.add("ollama")
    if project is None:
        return providers
    try:
        providers.update(k for k in project.provider_model_keys() if k in keyed)
    except Exception:
        pass
    try:
        root = project.path.parent
        providers.update(
            k for k in provider_config.resolve_effective_keys(root) if k in keyed
        )
    except Exception:
        pass
    if _project_network_off(project):
        # A keyed remote provider is not usable under the project's network
        # setting, so it must not make
        # model-backed engines look launchable.
        from frisket.ai.models.metadata import is_remote_provider

        providers = {p for p in providers if not is_remote_provider(p)}
    return providers


def _project_network_off(project: Project | None) -> bool:
    if project is None:
        return False
    try:
        return project.effective_network_policy() == "off"
    except Exception:
        return False


NETWORK_OFF_ENGINE_ERROR = (
    "This project's network setting is off; this engine calls an external "
    "service. Turn network on in the project settings to use it."
)


def _apply_network_off_engine_hints(
    engines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hosted-tier engines read as
    unavailable with a policy reason instead of a credential one. The ``llm``
    pseudo-engine is skipped — its availability already flows from
    ``_configured_llm_providers``, which drops remote providers under
    ``off``. The authoritative gate stays at validate/dispatch."""
    out: list[dict[str, Any]] = []
    for engine in engines:
        if engine.get("tier") == "hosted" and engine.get("id") != "llm":
            engine = {**engine, "available": False, "error": NETWORK_OFF_ENGINE_ERROR}
        out.append(engine)
    return out


def _transcribe_language_hint(engine_id: str) -> dict[str, Any]:
    from frisket.contracts.transcription_language import (
        transcribe_language_declaration,
    )

    return transcribe_language_declaration(engine_id).model_dump()


def _translate_language_hint(engine_id: str) -> dict[str, Any]:
    from frisket.actions.translation_languages import (
        translate_language_declaration,
    )

    return translate_language_declaration(engine_id).model_dump()


def _transcribe_diarization_hint(engine_id: str) -> dict[str, Any]:
    from frisket.contracts.actions.schemas.media import (
        transcribe_engine_capabilities,
        transcribe_diarization_mode,
        transcribe_max_speakers,
        transcribe_speaker_hint,
        transcribe_supports_diarization,
    )

    mode = transcribe_diarization_mode(engine_id)
    capabilities = transcribe_engine_capabilities(engine_id)
    supported = transcribe_supports_diarization(engine_id)
    hint: dict[str, Any] = {"supported": supported, "mode": mode}
    if supported:
        cap = transcribe_max_speakers(engine_id)
        if cap is not None:
            hint["max_speakers"] = cap
        hint["speaker_hint"] = transcribe_speaker_hint(engine_id)
        if capabilities.diarization_default:
            hint["default"] = True
    return hint


# Source for the generated frontend fallback; regenerate it after edits here.
OCR_VISION_ENGINES: tuple[dict[str, Any], ...] = (
    {
        "id": "openai/gpt-4.1-mini",
        "label": "OpenAI GPT-4.1 mini vision OCR (remote)",
        "billable": True,
    },
    {
        "id": "gemini/gemini-3.5-flash",
        "label": "Gemini 3.5 Flash vision OCR (remote)",
        "billable": True,
    },
    {
        "id": "openrouter/minimax/minimax-m3",
        "label": "MiniMax M3 document OCR (OpenRouter)",
        "billable": True,
    },
)


def _transcribe_options_hint(engine_id: str) -> dict[str, bool]:
    from frisket.contracts.actions.schemas.media import (
        transcribe_engine_capabilities,
    )

    capabilities = transcribe_engine_capabilities(engine_id)
    return {
        "language": capabilities.accepts_language,
        "vad": capabilities.vad,
        "model_size": capabilities.model_size,
        "context": capabilities.context,
        "clean": capabilities.clean,
    }


def _recipe_engines(
    action_kind: str,
    sidecar_capabilities: dict[str, Any],
    project: Project | None = None,
    org_provider_keys: Mapping[str, str] | None = None,
    has_local_model_endpoint: bool = False,
    effective_router: Any = None,
) -> list[dict[str, Any]]:
    if action_kind == "map.classify":
        from frisket.semantic import (
            PROVIDERLESS_CLASSIFY_MODEL,
            local_embedder,
        )
        from frisket.server.provider_config import ENV_VAR as PROVIDER_ENV_VAR

        local_error = None
        try:
            local_ok = (
                local_embedder(
                    PROVIDERLESS_CLASSIFY_MODEL,
                    capability="providerless_classify",
                )
                is not None
            )
        except ValueError as exc:
            local_ok = False
            local_error = str(exc)
        llm_providers = _configured_llm_providers(
            project,
            org_provider_keys,
            has_local_model_endpoint=has_local_model_endpoint,
            effective_router=effective_router,
        )
        return [
            _engine(
                "local_semantic",
                "Local semantic",
                tier="local",
                available=local_ok,
                error=(
                    None
                    if local_ok
                    else local_error
                    or "Local FastEmbed is unavailable; reinstall Frisket, unset FRISKET_DISABLE_LOCAL_EMBED, or explicitly enable providerless Classify."
                ),
                models=[PROVIDERLESS_CLASSIFY_MODEL] if local_ok else None,
            ),
            _engine(
                "llm",
                "Model",
                tier="hosted",
                billable=True,
                available=bool(llm_providers),
                error=(
                    None
                    if llm_providers
                    else "Configure a provider API key ("
                    + ", ".join(PROVIDER_ENV_VAR.values())
                    + ") to enable model classification."
                ),
            ),
        ]
    if action_kind == "enrich.geocode":
        from frisket.credentials import resolve_credential

        decl = _table_decls(GEOCODE_ENGINE_TABLE)
        opencage_available = bool(resolve_credential(project, "OPENCAGE_API_KEY"))
        return [
            _declared_engine(
                decl["opencage"],
                available=opencage_available,
                error=(
                    None
                    if opencage_available
                    else "Add OPENCAGE_API_KEY in Settings → Secrets to enable OpenCage."
                ),
                pricing=external_pricing_entry(GEOCODE_EXTERNAL_GEOCODER),
            ),
            _declared_engine(decl["nominatim"]),
        ]
    if action_kind == "enrich.census_demographics":
        [declaration] = CENSUS_ENGINE_TABLE
        return [_declared_engine(declaration)]
    if action_kind == "map.translate":
        from frisket.credentials import resolve_credential
        from frisket.server.provider_config import ENV_VAR as PROVIDER_ENV_VAR

        # Match execution's effective provider-key overlay.
        llm_providers = _configured_llm_providers(
            project,
            org_provider_keys,
            has_local_model_endpoint=has_local_model_endpoint,
            effective_router=effective_router,
        )
        deepl_key = resolve_credential(project, "DEEPL_API_KEY")
        google_key = resolve_credential(project, "GOOGLE_TRANSLATE_API_KEY")
        from frisket.ops.integrations import opus_mt as _opus_mt
        from frisket.ai.models import artifact_manifest as _artifact_manifest

        opus_pairs = _opus_mt.installed_pairs()
        opus_runtime = _opus_mt.runtime_available()
        opus_downloadable = [
            {
                "pair": entry.ref[len("opus-mt:") :],
                "display_name": entry.display_name,
                "size": entry.total_size,
                "license": entry.license,
            }
            for entry in _artifact_manifest.all_pinned()
            if entry.kind == "ct2_pair" and entry.ref.startswith("opus-mt:")
        ]
        # Keep the engine selectable with zero pairs so installation remains reachable.
        opus_error: str | None = None if opus_runtime else _opus_mt.REMEDIATION
        opus_engine = _engine(
            "opus_mt",
            "Opus-MT local translation (per-language-pair)",
            tier="local",
            available=opus_runtime,
            error=opus_error,
            models=opus_pairs or None,
            language=_translate_language_hint("opus_mt"),
        )
        opus_engine["downloadable_pairs"] = opus_downloadable

        from frisket.ops.integrations import hy_mt2 as _hy_mt2

        hy_runtime = _hy_mt2.runtime_available()
        hy_installed = _hy_mt2.is_installed()
        hy_pinned = _artifact_manifest.hy_mt2_artifact()
        hy_mt2_engine = _engine(
            "hy_mt2",
            "Hy-MT2 local translation (experimental)",
            tier="local",
            available=hy_runtime,
            error=None if hy_runtime else _hy_mt2.REMEDIATION,
            language=_translate_language_hint("hy_mt2"),
        )
        if hy_pinned is not None:
            hy_mt2_engine["downloadable_model"] = {
                "display_name": hy_pinned.display_name,
                "size": hy_pinned.total_size,
                "license": hy_pinned.license,
                "installed": hy_installed,
            }
        return [
            _engine(
                "llm",
                "LLM translation (uses your model)",
                tier="hosted",
                billable=True,
                available=bool(llm_providers),
                error=(
                    None
                    if llm_providers
                    else "Configure a provider API key ("
                    + ", ".join(PROVIDER_ENV_VAR.values())
                    + ") to enable LLM translation."
                ),
                language=_translate_language_hint("llm"),
            ),
            _engine(
                "deepl",
                "DeepL API (remote)",
                tier="hosted",
                billable=True,
                available=bool(deepl_key),
                error=(
                    None
                    if deepl_key
                    else "Add DEEPL_API_KEY in Settings → Secrets to enable DeepL translation."
                ),
                language=_translate_language_hint("deepl"),
            ),
            _engine(
                "google_translate",
                "Google Cloud Translation (remote)",
                tier="hosted",
                billable=True,
                available=bool(google_key),
                error=(
                    None
                    if google_key
                    else "Add GOOGLE_TRANSLATE_API_KEY in Settings → Secrets "
                    "to enable Google Cloud Translation."
                ),
                language=_translate_language_hint("google_translate"),
            ),
            opus_engine,
            hy_mt2_engine,
        ]
    if action_kind == "media.transcribe":
        from frisket.execution.definitions import (
            LOCAL_WHISPER_SIZES,
            faster_whisper_runtime_present,
            parakeet_artifacts_present,
            parakeet_runtime_present,
        )

        configured_providers = _configured_llm_providers(
            project,
            org_provider_keys,
            has_local_model_endpoint=has_local_model_endpoint,
            effective_router=effective_router,
        )
        openai_available = "openai" in configured_providers
        openrouter_available = "openrouter" in configured_providers

        turbo_ok, turbo_err, turbo_models = _sidecar_v1_engine(
            sidecar_capabilities,
            route="/v1/transcribe",
            name="whisper-turbo",
            contract_version="frisket.transcription.v1",
        )
        parakeet_gateway_ok, parakeet_gateway_err, parakeet_gateway_models = (
            _sidecar_v1_engine(
                sidecar_capabilities,
                route="/v1/transcribe",
                name="parakeet-tdt",
                contract_version="frisket.transcription.v1",
            )
        )
        decl = _table_decls(TRANSCRIBE_ENGINE_TABLE)
        mai = decl["openrouter/microsoft/mai-transcribe-2"]
        parakeet_runtime_ok = parakeet_runtime_present()
        parakeet_local_ok = parakeet_runtime_ok and parakeet_artifacts_present()
        parakeet_local_err = (
            None
            if parakeet_local_ok
            else (
                "Install Frisket's standard tier in the environment that runs "
                "the app (pip install 'frisket-data[standard]'), then restart Frisket."
                if not parakeet_runtime_ok
                else "Download the pinned Parakeet + Silero models before "
                "running local Parakeet transcription."
            )
        )
        parakeet_engine = _declared_engine(
            decl["parakeet-tdt"],
            available=parakeet_local_ok or parakeet_gateway_ok,
            error=(
                None
                if parakeet_local_ok or parakeet_gateway_ok
                else parakeet_local_err or parakeet_gateway_err
            ),
            language=_transcribe_language_hint("parakeet-tdt"),
            diarization=_transcribe_diarization_hint("parakeet-tdt"),
            transcription_options=_transcribe_options_hint("parakeet-tdt"),
        )
        parakeet_engine["targets"] = [
            {
                "target": "local-onnx",
                "available": parakeet_local_ok,
                "error": parakeet_local_err,
                "diarization": {"supported": False, "mode": "none"},
            },
            {
                "target": "models-gateway",
                "available": parakeet_gateway_ok,
                "error": parakeet_gateway_err,
                "models": parakeet_gateway_models,
                "diarization": _transcribe_diarization_hint("parakeet-tdt"),
            },
        ]
        faster_whisper_ok = faster_whisper_runtime_present()
        faster_whisper_err = (
            None
            if faster_whisper_ok
            else (
                "Install Frisket's standard tier in the environment that runs "
                "the app (pip install 'frisket-data[standard]'), then restart Frisket."
            )
        )
        faster_whisper_engine = _declared_engine(
            decl["faster_whisper"],
            available=faster_whisper_ok,
            error=faster_whisper_err,
            language=_transcribe_language_hint("faster_whisper"),
            diarization=_transcribe_diarization_hint("faster_whisper"),
            transcription_options=_transcribe_options_hint("faster_whisper"),
        )
        faster_whisper_engine["targets"] = [
            {
                "target": "local",
                "available": faster_whisper_ok,
                "error": faster_whisper_err,
                "sizes": list(LOCAL_WHISPER_SIZES),
            }
        ]
        whisper_turbo_engine = _declared_engine(
            decl["whisper-turbo"],
            available=turbo_ok,
            error=turbo_err,
            models=turbo_models,
            language=_transcribe_language_hint("whisper-turbo"),
            diarization=_transcribe_diarization_hint("whisper-turbo"),
            transcription_options=_transcribe_options_hint("whisper-turbo"),
        )
        whisper_turbo_engine["targets"] = [
            {
                "target": "models-gateway",
                "available": turbo_ok,
                "error": turbo_err,
                "models": turbo_models,
            }
        ]
        from frisket.ai.models import artifact_manifest as _artifact_manifest

        parakeet_downloadable = [
            {
                "ref": entry.ref,
                "display_name": human_name,
                "revision": entry.hf_snapshot.revision,
                "size": entry.approx_size_bytes,
                "license": entry.license,
            }
            for entry, human_name in (
                (
                    _artifact_manifest.parakeet_model_artifact(),
                    "Parakeet transcription model",
                ),
                (
                    _artifact_manifest.parakeet_vad_artifact(),
                    "Silero voice-activity model",
                ),
            )
            if entry is not None and entry.hf_snapshot is not None
        ]
        if parakeet_downloadable:
            parakeet_engine["downloadable_models"] = parakeet_downloadable
        whisper_entry = _artifact_manifest.whisper_base_artifact()
        if whisper_entry is not None and whisper_entry.hf_snapshot is not None:
            faster_whisper_engine["downloadable_models"] = [
                {
                    "ref": whisper_entry.ref,
                    "display_name": whisper_entry.display_name,
                    "revision": whisper_entry.hf_snapshot.revision,
                    "size": whisper_entry.approx_size_bytes,
                    "license": whisper_entry.license,
                }
            ]
        moss_ok, moss_err, moss_models = _sidecar_v1_engine(
            sidecar_capabilities,
            route="/v1/transcribe",
            name="moss",
            contract_version="frisket.transcription.v1",
        )
        vibevoice_ok, vibevoice_err, vibevoice_models = _sidecar_v1_engine(
            sidecar_capabilities,
            route="/v1/transcribe",
            name="vibevoice-asr",
            contract_version="frisket.transcription.v1",
        )
        return [
            faster_whisper_engine,
            whisper_turbo_engine,
            parakeet_engine,
            _declared_engine(
                decl["moss"],
                available=moss_ok,
                error=moss_err,
                models=moss_models,
                language=_transcribe_language_hint("moss"),
                diarization=_transcribe_diarization_hint("moss"),
                transcription_options=_transcribe_options_hint("moss"),
            ),
            _declared_engine(
                decl["vibevoice-asr"],
                available=vibevoice_ok,
                error=vibevoice_err,
                models=vibevoice_models,
                language=_transcribe_language_hint("vibevoice-asr"),
                diarization=_transcribe_diarization_hint("vibevoice-asr"),
                transcription_options=_transcribe_options_hint("vibevoice-asr"),
            ),
            _engine(
                "openai/whisper-1",
                "OpenAI Whisper (remote provider API)",
                tier="hosted",
                billable=True,
                available=openai_available,
                error=(
                    None
                    if openai_available
                    else "Configure an OpenAI API key to enable OpenAI Whisper."
                ),
                language=_transcribe_language_hint("openai/whisper-1"),
                diarization=_transcribe_diarization_hint("openai/whisper-1"),
                transcription_options=_transcribe_options_hint("openai/whisper-1"),
            ),
            _declared_engine(
                mai,
                available=openrouter_available,
                error=(
                    None
                    if openrouter_available
                    else "Configure an OpenRouter API key to enable Microsoft MAI-Transcribe 2."
                ),
                language=_transcribe_language_hint(mai.id),
                diarization=_transcribe_diarization_hint(mai.id),
                transcription_options=_transcribe_options_hint(mai.id),
            ),
        ]
    if action_kind == "media.ocr":
        from frisket.credentials import resolve_credential
        from frisket.ops.ocr_engines import (
            rapidocr_available,
            tesseract_available,
        )

        dots_ok, dots_err, dots_models = _sidecar_engine(
            sidecar_capabilities, route="/ocr", name="dots.mocr"
        )
        glm_ok, glm_err, glm_models = _sidecar_engine(
            sidecar_capabilities, route="/ocr", name="glm-ocr"
        )
        surya_ok, surya_err, surya_models = _sidecar_engine(
            sidecar_capabilities, route="/ocr", name="surya2"
        )
        paddle_ok, paddle_err, paddle_models = _sidecar_engine(
            sidecar_capabilities, route="/ocr", name="paddleocr-vl"
        )
        pp_ocr_ok, pp_ocr_err, pp_ocr_models = _sidecar_engine(
            sidecar_capabilities, route="/ocr", name="pp-ocrv6"
        )
        tess_ok, tess_err = tesseract_available()
        rapid_ok, rapid_err = rapidocr_available()
        ocr_llm_providers = _configured_llm_providers(
            project,
            org_provider_keys,
            has_local_model_endpoint=has_local_model_endpoint,
            effective_router=effective_router,
        )
        openai_available = "openai" in ocr_llm_providers
        gemini_available = "gemini" in ocr_llm_providers
        openrouter_available = "openrouter" in ocr_llm_providers
        datalab_key = resolve_credential(project, "DATALAB_API_KEY")
        decl = _table_decls(OCR_ENGINE_TABLE)
        # Projector parity pins ``dots.mocr`` at index 3.
        return [
            _declared_engine(
                decl["rapidocr"],
                available=rapid_ok,
                error=rapid_err,
            ),
            _declared_engine(
                decl["tesseract"],
                available=tess_ok,
                error=tess_err,
                language={
                    "mode": "fixed",
                    "default": "en",
                    "fixed_language": "en",
                    "choices": [{"value": "en", "label": "English"}],
                    "detects": False,
                },
            ),
            _declared_engine(
                decl["paddleocr-vl"],
                available=paddle_ok,
                error=paddle_err,
                models=paddle_models,
            ),
            _declared_engine(
                decl["dots.mocr"],
                available=dots_ok,
                error=dots_err,
                models=dots_models,
            ),
            _declared_engine(
                decl["pp-ocrv6"],
                available=pp_ocr_ok,
                error=pp_ocr_err,
                models=pp_ocr_models,
            ),
            _declared_engine(
                decl["glm-ocr"],
                available=glm_ok,
                error=glm_err,
                models=glm_models,
            ),
            _declared_engine(
                decl["surya2"],
                available=surya_ok,
                error=surya_err,
                models=surya_models,
            ),
            _engine(
                OCR_VISION_ENGINES[0]["id"],
                OCR_VISION_ENGINES[0]["label"],
                tier="hosted",
                billable=OCR_VISION_ENGINES[0]["billable"],
                available=openai_available,
                error=None
                if openai_available
                else "missing OpenAI key (env OPENAI_API_KEY or Settings → AI Providers)",
                models=["gpt-4.1-mini"],
            ),
            _engine(
                OCR_VISION_ENGINES[1]["id"],
                OCR_VISION_ENGINES[1]["label"],
                tier="hosted",
                billable=OCR_VISION_ENGINES[1]["billable"],
                available=gemini_available,
                error=None
                if gemini_available
                else "missing Gemini key (env GEMINI_API_KEY or Settings → AI Providers)",
                models=["gemini-3.5-flash"],
            ),
            _engine(
                OCR_VISION_ENGINES[2]["id"],
                OCR_VISION_ENGINES[2]["label"],
                tier="hosted",
                billable=OCR_VISION_ENGINES[2]["billable"],
                available=openrouter_available,
                error=None
                if openrouter_available
                else "missing OpenRouter key (env OPENROUTER_API_KEY or Settings → AI Providers)",
                models=["minimax/minimax-m3"],
            ),
            # Datalab hosted OCR: the vendor's own hosted product
            # (not one of their open models), so NO license hint — pricing
            # rides the shared external_pricing catalog for pay-per-call
            # display (DATALAB_OCR_PAGE).
            _declared_engine(
                decl["datalab"],
                available=bool(datalab_key),
                error=(
                    None
                    if datalab_key
                    else "Add DATALAB_API_KEY in Settings → Secrets to enable Datalab hosted OCR."
                ),
                pricing=external_pricing_entry(DATALAB_OCR_PAGE),
            ),
        ]
    if action_kind == "media.to_markdown":
        from frisket.credentials import resolve_credential

        ok, err, models = _sidecar_engine(
            sidecar_capabilities, route="/to-markdown", name="docling"
        )
        # Chandra 2 (Datalab, github.com/datalab-to/chandra) — a second,
        # experimental engine on the same /to-markdown sidecar route as
        # docling. Unavailable plus
        # remediation when the sidecar lacks it, using the same
        # _sidecar_engine fallback as the other sidecar engines.
        chandra_ok, chandra_err, chandra_models = _sidecar_engine(
            sidecar_capabilities, route="/to-markdown", name="chandra"
        )
        # Datalab's hosted Marker API: a remote, pay-per-call tier
        # — same vendor as chandra's open weights, now also their paid hosted
        # product. resolve_credential (env -> project secrets, two sources —
        # no org-level injection point exists for this credential; see the
        # longer note on the "ocr" branch's matching datalab_key above).
        datalab_key = resolve_credential(project, "DATALAB_API_KEY")
        decl = _table_decls(TO_MARKDOWN_ENGINE_TABLE)
        return [
            _declared_engine(decl["markitdown"]),
            _declared_engine(decl["trafilatura_html"]),
            _declared_engine(
                decl["docling"],
                available=ok,
                error=err,
                models=models,
            ),
            _declared_engine(
                decl["chandra"],
                available=chandra_ok,
                error=chandra_err,
                models=chandra_models,
                # Chandra's weights ship under
                # Datalab's Modified OpenRAIL-M, not the repo's Apache-2.0
                # code license — restricted (revenue/funding threshold +
                # non-compete), so it's the first flagged catalog entry.
                # Verified path: github.com/datalab-to/chandra's default
                # branch is `master` (not `main`); MODEL_LICENSE (not
                # LICENSE, which covers only the surrounding code) is the
                # model's own license file. Disclosure is done in-product
                # here; weight
                # installation remains the operator's license responsibility.
                license_hint={
                    "name": "Modified OpenRAIL-M (Datalab)",
                    "url": "https://github.com/datalab-to/chandra/blob/master/MODEL_LICENSE",
                    "note": (
                        "Free under $2M revenue/funding; use must not "
                        "compete with Datalab products."
                    ),
                    "restricted": True,
                },
            ),
            # Datalab hosted Marker: the vendor's own hosted
            # product, so NO license hint — pricing rides the shared
            # external_pricing catalog for pay-per-call display
            # (DATALAB_CONVERT_PAGE).
            _declared_engine(
                # The table label states the input restriction
                # (PDF/Office/image only — NOT the html/text
                # markitdown/trafilatura_html accept) since
                # ConvertMarkdownRecipe._convert_datalab rejects unsupported
                # inputs before any network call; this is the catalog-copy
                # half of that fix.
                decl["datalab"],
                available=bool(datalab_key),
                error=(
                    None
                    if datalab_key
                    else "Add DATALAB_API_KEY in Settings → Secrets to enable Datalab hosted document conversion."
                ),
                pricing=external_pricing_entry(DATALAB_CONVERT_PAGE),
            ),
        ]
    if action_kind == "map.ner":
        # local import (launcher-safe): spacy_available() is a cheap presence
        # probe (find_spec only, never spacy.load), shared with the recipe so
        # catalog == runtime, mirroring tesseract_available for OCR.
        from frisket.ops.spacy_ner import SPACY_MODEL, spacy_available
        from frisket.server.provider_config import ENV_VAR as PROVIDER_ENV_VAR

        spacy_ok, spacy_err = spacy_available()
        ok, err, models = _sidecar_engine(
            sidecar_capabilities, route="/ner", name="gliner"
        )
        llm_providers = _configured_llm_providers(
            project,
            org_provider_keys,
            has_local_model_endpoint=has_local_model_endpoint,
            effective_router=effective_router,
        )
        spacy_engine = _engine(
            "spacy",
            "spaCy named-entity extraction (local, fixed labels)",
            tier="local",
            available=spacy_ok,
            error=spacy_err,
            models=[SPACY_MODEL] if spacy_ok else None,
        )
        import importlib.util

        from frisket.ai.models import artifact_manifest as _artifact_manifest

        spacy_artifact = _artifact_manifest.spacy_model_artifact()
        if spacy_artifact is not None and importlib.util.find_spec("spacy") is not None:
            spacy_engine["downloadable_models"] = [
                {
                    "ref": spacy_artifact.ref,
                    "display_name": spacy_artifact.display_name,
                    "revision": "3.8.0",
                    "size": spacy_artifact.total_size,
                    "license": spacy_artifact.license,
                }
            ]
        return [
            spacy_engine,
            _engine(
                "gliner",
                "GLiNER named-entity extraction (sidecar)",
                tier="sidecar",
                available=ok,
                error=err,
                models=models,
            ),
            _engine(
                "llm",
                "LLM named-entity extraction (remote, extract-shaped)",
                tier="hosted",
                billable=True,
                available=bool(llm_providers),
                error=(
                    None
                    if llm_providers
                    else "Configure a provider API key ("
                    + ", ".join(PROVIDER_ENV_VAR.values())
                    + ") to enable the LLM entities engine."
                ),
            ),
        ]
    if action_kind == "media.ytdlp_download":
        # The yt-dlp package installed in Frisket's Python environment is the
        # sole runtime. Missing-package state is a real launch gate; version
        # telemetry lives on the dedicated runtime-status endpoint.
        status = ytdlp.managed_runtime_status()
        return [
            _engine(
                ytdlp.YTDLP_ENGINE_ID,
                "yt-dlp media extractor",
                tier="local",
                available=bool(status.get("available")),
                error=status.get("error"),
            ),
        ]
    return []


def _recipe_pricing(name: str) -> dict[str, Any] | None:
    if name == "geocode":
        if not os.environ.get("OPENCAGE_API_KEY"):
            # Nominatim is a free public API, not an unavailable/zero-dollar
            # tariff. Its external-egress truth lives in the action contract
            # and runtime facts; there is no pricing hint to render.
            return None
        return external_pricing_entry(GEOCODE_EXTERNAL_GEOCODER)
    return None


def _recipe_pricing_options(name: str) -> dict[str, dict[str, Any]] | None:
    if name == "geocode":
        return {
            "opencage": external_pricing_entry(GEOCODE_EXTERNAL_GEOCODER),
        }
    return None


def _recipe_cost_source(name: str) -> str | None:
    """Non-price cost provenance for an action's default selection."""
    if name == "census_demographics":
        return "free_public_api"
    if name == "geocode" and not os.environ.get("OPENCAGE_API_KEY"):
        return "free_public_api"
    return None


def _recipe_cost_source_options(name: str) -> dict[str, str] | None:
    """Non-price provenance for explicit choices omitted from price options."""
    if name == "geocode":
        return {"nominatim": "free_public_api"}
    return None


def _composition_pricing_for_engine(
    composition: ExecutionComposition,
    *,
    capability: str,
    engine: str,
    provider_pricing: dict[str, Any],
) -> dict[str, Any] | None:
    """Project one engine's price through the same composition as targets.

    Provider-direct pricing survives for open/own-key candidates.  A filtered
    commercial target contributes no option at all; an injected offer replaces
    the provider tariff with its exact request-scoped terms.
    """

    candidates = _composition_targets_for_engine(
        composition,
        capability=capability,
        engine=engine,
    )
    if not candidates:
        return None
    for target, _support in candidates:
        offering = composition.offering_for(
            target_id=target.id,
            capability=capability,
            engine=engine,
        )
        if offering is not None:
            return _commercial_pricing_hint(target=target, offering=offering)
    return provider_pricing


def _sanitize_action_hint(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            item_key: _sanitize_action_hint(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_action_hint(item, key=key) for item in value]
    if isinstance(value, str) and key in ACTION_HINT_PROSE_KEYS:
        return re.sub(
            r"\b[Rr]ecipes?\b",
            lambda m: "Actions" if m.group(0)[0].isupper() else "actions",
            value,
        )
    return value


def _url_classification_launcher_hints() -> dict[str, dict[str, Any]]:
    """Reverse-index the first-party URL matchers by their handler action_kind.

    The URL classification registry is a per-cell-VALUE affordance
    provider: a URL cell whose classification routes to action X surfaces X as
    its recommended default. Emitting each action's ``recommended_for`` matcher
    metadata into the catalog ui_hints lets the web affordance layer classify a
    URL cell client-side from the same table the backend re-validates against,
    so frontend hints and backend routing cannot drift. Hints only — matcher
    METADATA, never executable predicates.
    """
    from frisket.features.url_classification.contract import (
        SNAPSHOT_SCHEMA_VERSION,
        build_snapshot,
    )

    by_action: dict[str, list[dict[str, Any]]] = {}
    for metadata in build_snapshot()["matchers"]:
        action_kind = metadata.get("handler_action_kind")
        if not action_kind:
            continue
        by_action.setdefault(action_kind, []).append(metadata)
    return {
        action_kind: {
            "url_classification": {
                "schema_version": SNAPSHOT_SCHEMA_VERSION,
                "recommended_for": sorted(matchers, key=lambda md: md["matcher_id"]),
            }
        }
        for action_kind, matchers in by_action.items()
    }


def project_action_catalog_launcher_hints(
    sidecar_capabilities: dict[str, Any],
    project: Project | None = None,
    org_provider_keys: Mapping[str, str] | None = None,
    execution_composition: ExecutionComposition | None = None,
    has_local_model_endpoint: bool = False,
    effective_router: Any = None,
) -> dict[str, dict[str, Any]]:
    launcher_hints: dict[str, dict[str, Any]] = {}
    network_off = _project_network_off(project)
    from frisket.actions.registry import ACTION_REGISTRY
    from frisket.actions.core import ModelRows, ROUTED_CAPABILITIES, routed_capability
    from frisket.actions.media_download_types import MediaDownloader

    providers = [
        (
            spec.action_kind,
            spec.recipe.name,
            bool(spec.recipe.llm),
            spec.recipe.params(),
            getattr(spec.recipe, "execution_capability", None),
        )
        for spec in default_registry().recipe_specs()
        if spec.action_kind
    ]
    providers.extend(
        (registered.action_id, registered.action_id.rsplit(".", 1)[-1], True, [], None)
        for registered in ACTION_REGISTRY.actions
        if isinstance(registered.definition.run, ModelRows)
    )
    providers.extend(
        (registered.action_id, "media.ytdlp_download", False, [], None)
        for registered in ACTION_REGISTRY.actions
        if MediaDownloader in getattr(registered.definition.run, "capabilities", ())
    )
    providers.extend(
        (
            registered.action_id,
            ROUTED_CAPABILITIES[capability],
            False,
            [],
            ROUTED_CAPABILITIES[capability],
        )
        for registered in ACTION_REGISTRY.actions
        if (capability := routed_capability(registered.definition.run)) is not None
    )
    for (
        action_kind,
        provider_name,
        uses_model,
        form_params,
        execution_capability,
    ) in providers:
        hints: dict[str, Any] = {
            "uses_model": uses_model,
            "form_params": form_params,
        }
        engines = _recipe_engines(
            f"enrich.{execution_capability}"
            if execution_capability in {"geocode", "census_demographics"}
            else "media.to_markdown"
            if execution_capability == "document.convert"
            else f"media.{execution_capability}"
            if execution_capability in {"ocr", "transcribe"}
            else action_kind,
            sidecar_capabilities,
            project=project,
            org_provider_keys=org_provider_keys,
            has_local_model_endpoint=has_local_model_endpoint,
            effective_router=effective_router,
        )
        engines = _project_execution_composition_engines(
            engines,
            capability=execution_capability,
            composition=execution_composition,
            declared_gateway_fallback=bool(
                sidecar_capabilities.get("transient_failure")
                or sidecar_capabilities.get("catalog_from_composition")
            ),
        )
        if engines:
            if network_off:
                engines = _apply_network_off_engine_hints(engines)
            hints["engines"] = engines
        pricing = _recipe_pricing(provider_name)
        if (
            pricing is not None
            and provider_name == "geocode"
            and execution_composition is not None
            and execution_capability is not None
        ):
            pricing = _composition_pricing_for_engine(
                execution_composition,
                capability=execution_capability,
                engine="opencage",
                provider_pricing=pricing,
            )
        if pricing:
            hints["pricing"] = pricing
        pricing_options = _recipe_pricing_options(provider_name)
        if (
            pricing_options
            and execution_composition is not None
            and execution_capability is not None
        ):
            pricing_options = {
                engine: projected
                for engine, value in pricing_options.items()
                if (
                    projected := _composition_pricing_for_engine(
                        execution_composition,
                        capability=execution_capability,
                        engine=engine,
                        provider_pricing=value,
                    )
                )
                is not None
            }
        if pricing_options:
            hints["pricing_options"] = pricing_options
        cost_source = _recipe_cost_source(provider_name)
        if cost_source:
            hints["cost_source"] = cost_source
        cost_source_options = _recipe_cost_source_options(provider_name)
        if cost_source_options:
            hints["cost_source_options"] = cost_source_options
        default_output = ACTION_PROVIDER_DEFAULT_OUTPUT.get(provider_name)
        if default_output:
            hints["default_output"] = default_output
        launcher_hints[action_kind] = _sanitize_action_hint(hints)
    for action_kind, extra in _url_classification_launcher_hints().items():
        target = launcher_hints.setdefault(action_kind, {})
        target.update(_sanitize_action_hint(extra))
    from frisket.features.topic_segmentation.engines import engine_catalog

    launcher_hints.setdefault("map.find_topic_sections", {})["engines"] = list(
        engine_catalog()
    )
    return launcher_hints


def action_catalog_payload_with_launcher_hints(
    sidecar_capabilities: dict[str, Any],
    project: Project | None = None,
    org_provider_keys: Mapping[str, str] | None = None,
    execution_composition: ExecutionComposition | None = None,
    has_local_model_endpoint: bool = False,
    effective_router: Any = None,
) -> dict[str, Any]:
    launcher_hints = project_action_catalog_launcher_hints(
        sidecar_capabilities,
        project=project,
        org_provider_keys=org_provider_keys,
        execution_composition=execution_composition,
        has_local_model_endpoint=has_local_model_endpoint,
        effective_router=effective_router,
    )
    payload = root_action_catalog_payload()
    collisions: list[tuple[str, list[str]]] = []
    for entry in payload["actions"]:
        hints = launcher_hints.get(entry["kind"])
        if hints:
            definition_hints = entry.get("ui_hints") or {}
            offending_keys = sorted(
                key
                for key in definition_hints.keys() & hints.keys()
                if (entry["kind"], key)
                not in ACTION_CATALOG_LAUNCHER_HINT_COLLISION_ALLOW_SET
            )
            if offending_keys:
                collisions.append((entry["kind"], offending_keys))
    if collisions:
        details = "; ".join(
            f"{kind}: {offending_keys}" for kind, offending_keys in collisions
        )
        raise ValueError(f"action catalog launcher hint collisions: {details}")
    for entry in payload["actions"]:
        hints = launcher_hints.get(entry["kind"])
        if hints:
            entry["ui_hints"] = {**(entry.get("ui_hints") or {}), **hints}
    return payload


def _apply_missing_credential_hints(
    project: Project,
    actions: list[dict[str, Any]],
) -> None:
    """action-api-key-gate-v1: project-aware `ui_hints.missing_credentials`
    per action, so ActionPanel can show the needs-credential gate (+ the
    Settings deep link) BEFORE a run is attempted — the SAME resolution
    (env var, then the project secrets store) the authoritative queue-time
    gate uses (server/action_enqueue.py), just read-only here."""
    for entry in actions:
        required = entry.get("required_credentials")
        if not required:
            continue
        missing = missing_required_credentials(project, required)
        if missing:
            entry["ui_hints"] = {
                **(entry.get("ui_hints") or {}),
                "missing_credentials": missing,
            }


def _apply_network_policy_hints(
    project: Project,
    actions: list[dict[str, Any]],
) -> None:
    """When the project's effective
    network policy is off, every kind whose STATIC catalog
    ``required_capabilities`` carries an ``external:*`` tag (the always-remote
    class) gets ``ui_hints.network_disabled`` so the launcher can gray it out
    with a policy reason before a run is attempted. Read-only mirror of the
    authoritative check in ActionRunService._network_disabled_result."""
    if not _project_network_off(project):
        return
    for entry in actions:
        external = [
            str(capability)
            for capability in entry.get("required_capabilities") or []
            if str(capability).startswith("external:")
        ]
        if external:
            entry["ui_hints"] = {
                **(entry.get("ui_hints") or {}),
                "network_disabled": True,
            }


# Kinds gated on a per-composition connected-account resolver
# (ExecutorDeps.connected_account_resolver — engine/executor/action_inventory.py)
# rather than a per-project secret. Today there's exactly one: the bare local
# single-user tier (server/app.py's create_app with no executor_deps_factory)
# structurally has no OAuth flow to connect a Google account at all, so
# export.google_sheets can never complete there even though it's registered
# unconditionally in the catalog (actions/exports.py). A second connected-account
# action would extend this
# dict rather than duplicate the function below.
_CONNECTED_ACCOUNT_UNAVAILABLE_REASONS: dict[str, str] = {
    "export.google_sheets": (
        "Google Sheets export needs a connected Google account, and this "
        "single-user tier has no OAuth flow to connect one. Available on "
        "the Team edition."
    ),
}


def _apply_connected_account_hints(
    actions: list[dict[str, Any]],
    *,
    connected_account_resolver_configured: bool,
) -> None:
    """Structural edition/composition gate, not a per-project probe: whether
    THIS app composition wired a connected-account resolver at all (team/
    hosted do; the bare local tier never does — server/routes/actions.py
    passes `workspace.executor_deps_factory is not None`). Read-only mirror
    of the authoritative run-time check in
    engine/executor/action_families/exports.py's `_run_export_google_sheets`
    (raises `connected_account_not_found` there when the resolver is None or
    returns nothing) — this hint fires BEFORE that call is ever attempted,
    same "read-only mirror of an authoritative gate" pattern as
    `_apply_missing_credential_hints`/`_apply_network_policy_hints` above."""
    if connected_account_resolver_configured:
        return
    for entry in actions:
        reason = _CONNECTED_ACCOUNT_UNAVAILABLE_REASONS.get(entry.get("kind", ""))
        if reason:
            entry["ui_hints"] = {
                **(entry.get("ui_hints") or {}),
                "unavailable_reason": reason,
            }


def project_action_catalog_payload_with_launcher_hints(
    project: Project,
    *,
    sidecar_capabilities: dict[str, Any],
    org_provider_keys: Mapping[str, str] | None = None,
    connected_account_resolver_configured: bool = True,
    execution_composition: ExecutionComposition | None = None,
    has_local_model_endpoint: bool = False,
    effective_router: Any = None,
) -> dict[str, Any]:
    payload = action_catalog_payload_with_launcher_hints(
        sidecar_capabilities,
        project=project,
        org_provider_keys=org_provider_keys,
        execution_composition=execution_composition,
        has_local_model_endpoint=has_local_model_endpoint,
        effective_router=effective_router,
    )
    payload["actions"] = [
        *payload["actions"],
        *_project_plugin_action_catalog_entries(project),
    ]
    payload["actions"] = sorted(payload["actions"], key=lambda entry: entry["kind"])
    _apply_missing_credential_hints(project, payload["actions"])
    _apply_network_policy_hints(project, payload["actions"])
    _apply_connected_account_hints(
        payload["actions"],
        connected_account_resolver_configured=connected_account_resolver_configured,
    )
    return payload


def _project_plugin_action_catalog_entries(project: Project) -> list[dict[str, Any]]:
    from frisket.authoring.workbench.plugin_runtime_capabilities import (
        project_runtime_binding,
    )

    entries: list[dict[str, Any]] = []
    for spec in default_registry().runtime_binding_specs("actions"):
        binding = project_runtime_binding(
            project, binding_type="actions", kind=spec.kind
        )
        if binding is None or binding.handler_api != "plugin_typed_action_native":
            continue
        entries.append(_plugin_action_catalog_entry(binding))
    return entries


def _plugin_action_catalog_entry(binding: RuntimeBindingSpec) -> dict[str, Any]:
    metadata = dict(binding.metadata or {})
    entry = dict(metadata["catalog_entry"])
    if "action_ui" in metadata:
        entry["ui_hints"] = {
            **entry["ui_hints"],
            "action_ui": dict(metadata["action_ui"]),
        }
    return entry


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]
