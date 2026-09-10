"""Translation provider adapters shared by admitted runs and local previews."""

from __future__ import annotations

from typing import Any

from frisket.actions.translate_types import translation_text
from frisket.ai.models.metadata import ModelCallMeta
from frisket.execution.attempt import routed_admission_in_scope
from frisket.execution.credential_use import (
    CredentialUseRefusal,
    require_consented_credential,
)
from frisket.ops.base import OpContext, RecipeInvocationHalt
from frisket.ops.integrations.translate_common import TranslateEngineError


class TranslationEngine:
    """Existing hosted/local engines, producing logical values and actual facts."""

    @staticmethod
    def _translation_column(spec: dict) -> str:
        return "translation"

    @staticmethod
    def _detected_column(spec: dict) -> str:
        return "detected_language"

    @staticmethod
    def _save_detected(spec: dict) -> bool:
        return bool(spec.get("save_detected_language"))

    def _shaped_result(
        self,
        spec: dict,
        translation: str,
        detected_raw: Any,
        *,
        cost: float | None = None,
        cost_unknown: bool = False,
        model_calls: list[dict[str, Any]] | None = None,
    ) -> Any:
        """Common output shaping for every non-LLM engine branch, so an added
        engine (opus_mt) only produces (translation, detected_raw, cost) and
        reuses this — the detected column is emitted ONLY when
        save_detected_language, under the derived name, and the per-row cost
        rides the (data, meta) channel MapRunner records.

        Three cost states, kept distinguishable (F6, never a silent zero):
        - free/local engine (opus_mt): ``cost=None`` and NOT ``cost_unknown``
          → no cost meta at all → MapRunner records the genuine 0.0.
        - billable, known rate: ``cost=<number>`` → recorded verbatim.
        - billable, UNKNOWN rate (a hosted engine whose per-character price is
          unconfigured): ``cost_unknown=True`` → meta carries ``cost=None`` +
          ``cost_source="unknown"`` so the spend reads as unknown, never a fake
          0 that would be mistaken for free.
        """
        from frisket.ops.integrations.translate_common import normalize_detected_bcp47

        data: dict[str, Any] = {self._translation_column(spec): translation}
        if self._save_detected(spec):
            data[self._detected_column(spec)] = normalize_detected_bcp47(detected_raw)
        if cost is None and not cost_unknown:
            return data
        meta: dict[str, Any] = {"cost": cost}
        if cost_unknown:
            meta["cost_source"] = "unknown"
        if model_calls:
            meta["model_calls"] = model_calls
        return data, meta

    @staticmethod
    def _source_language(spec: dict) -> str | None:
        """Resolve the single source-language hint the engine consumes.

        The wire param is a canonical list; every translate engine
        is single, so take the first (auto is [] -> None). No back-compat on
        the wire: the old scalar ``source_language`` is gone.
        """
        value = spec.get("language")
        if isinstance(value, list):
            value = value[0] if value else None
        if isinstance(value, str):
            value = value.strip()
        return value or None

    @staticmethod
    def _cancel_signal(ctx: "OpContext") -> Any:
        """The cooperative-cancel callable MapRunner threads into the row
        context (``ctx.extras['cancelled']``, a ``() -> bool``), or None. Passed
        into on-use provisioning so a cancelled run aborts a large pull promptly
        (finding F6) instead of blocking the row until the stream finishes."""
        extras = getattr(ctx, "extras", None)
        if isinstance(extras, dict):
            signal = extras.get("cancelled")
            if callable(signal):
                return signal
        return None

    @staticmethod
    def _require_nonempty(translation: str, engine: str) -> str:
        """F1 (eval failure-modes): a non-LLM engine returning an EMPTY
        translation over NON-EMPTY source text is a per-row FAILURE, not a
        terminal-ok empty cell.

        Every branch below reaches this only AFTER the empty-source early return
        in ``execute()`` (``if not text``), so the source is guaranteed non-empty
        here: an empty result therefore means the provider returned nothing, the
        GGUF degenerated, or DeepL sent an empty ``text`` — not a legitimately
        empty input. Previously ``""`` was written as a green ``ok`` cell that
        backfill never revisited (``_result_outcome`` maps a non-None value to
        ``ok``). The typed error routes through MapRunner's hosted-engine catch
        (``error_code="empty_output"``), which records the row as a TERMINAL
        failure (empty-output terminal-row contract): shown honestly as failed,
        never re-run by automatic backfill — whether a retry would fix it is
        not our judgment call — but re-runnable by a deliberate user retry
        (``run.backfill`` with explicit ``row_ids``). ``retryable=False``
        states that fact; MapRunner classifies on the CODE, not this flag.
        """

        if translation is None or not str(translation).strip():
            raise TranslateEngineError(
                code="empty_output",
                message=(
                    f"{engine} returned an empty translation for non-empty "
                    "source text; the row was not translated."
                ),
                retryable=False,
            )
        return translation

    @staticmethod
    def _source_text(row_values: dict[str, Any]) -> str:
        """The raw text to translate: the source cell value(s) joined, with NO
        `name: value` labels (those would themselves get translated). Images /
        blobs are skipped — hosted MT engines are text-only."""
        return translation_text(row_values)

    async def execute(
        self, row_values: dict[str, Any], spec: dict, ctx: "OpContext"
    ) -> Any:
        """Run hosted or local MT and return logical values with actual costs.

        Credentials, HTTP clients, and provisioning belong to this host adapter.
        The model branch is independently rendered by the typed action.
        """
        from frisket.credentials import resolve_credential_for_use
        from frisket.ops.integrations.translate_common import (
            hosted_translate_cost,
            normalize_language,
        )

        engine = spec.get("engine", "llm")
        if engine == "llm":
            raise RuntimeError("LLM translation uses the inspected model request.")
        text = self._source_text(row_values)
        if not text:
            return self._shaped_result(spec, "", None, cost=0.0)

        if engine in ("deepl", "google_translate"):
            key_name = (
                "DEEPL_API_KEY" if engine == "deepl" else "GOOGLE_TRANSLATE_API_KEY"
            )
            credential = resolve_credential_for_use(
                ctx.project,
                key_name,
                context=ctx.credential_use_context,
            )
            if credential is None:
                raise TranslateEngineError(
                    code="auth",
                    message=f"{key_name} is not configured (Settings → Secrets).",
                )
            admission = routed_admission_in_scope(ctx.extras)
            route = admission.route if admission is not None else None
            credential_context = ctx.credential_use_context
            cost_posture = (
                route.cost_posture
                if route is not None
                else (
                    credential_context.cost_posture
                    if credential_context is not None
                    else None
                )
            )
            try:
                require_consented_credential(
                    cost_posture=cost_posture,
                    selected_source=credential.source,
                    selected_owner=credential.owner,
                    context=credential_context,
                    effect=f"the hosted {engine} translation call",
                )
            except CredentialUseRefusal as exc:
                raise RecipeInvocationHalt(
                    "promise_violation",
                    f"{exc}; review the claims and re-consent to resume",
                ) from exc
            target = normalize_language(
                engine, str(spec.get("target_language", "English")), "target"
            )
            source = self._source_language(spec)
            source_code = None
            if source and source.lower() != "auto":
                source_code = normalize_language(engine, source, "source")
            if engine == "deepl":
                from frisket.ops.integrations.deepl import deepl_translate

                results = await deepl_translate(
                    ctx.http,
                    credential.value,
                    [text],
                    target,
                    source_code,
                    credential_source=credential.source,
                )
                # DeepL reports its own billed characters; fall back to source
                # length only if the field is absent.
                billed = results[0].get("billed_characters") if results else None
                char_count = billed if isinstance(billed, int) else len(text)
            else:
                from frisket.ops.integrations.google_translate import google_translate

                results = await google_translate(
                    ctx.http,
                    credential.value,
                    [text],
                    target,
                    source_code,
                    credential_source=credential.source,
                )
                # Google's v2 response has no billed field; it bills source
                # characters, so the source length is the count.
                char_count = len(text)
            first = results[0] if results else {}
            # Detected language: when an explicit source was given and the
            # provider omits detection (Google's explicit-source responses do),
            # fall back to the explicit source rather than null.
            detected_raw = first.get("detected_source_language")
            if detected_raw is None and source_code:
                detected_raw = source
            # Real/estimated spend from the pinned per-character rate. When the
            # rate is unconfigured hosted_translate_cost returns None -> record
            # the cost as UNKNOWN, never a fake 0.
            cost = hosted_translate_cost(engine, char_count)
            provider = "deepl" if engine == "deepl" else "google"
            fact = ModelCallMeta.provider_call(
                capability="translate",
                engine=engine,
                provider=provider,
                provider_kind="platform_api",
                credential_source=credential.source,
                provider_reported_cost_usd=None,
                provider_cost_usd=cost,
                cost_source="unknown" if cost is None else "configured_catalog",
                units={"characters": char_count, "requests": 1},
                warnings=[],
                duration_ms=None,
            ).as_dict()
            accounting = {
                "cost": cost,
                "model_calls": [fact],
                **({"cost_source": "unknown"} if cost is None else {}),
            }
            try:
                translation = self._require_nonempty(first.get("text", ""), engine)
            except TranslateEngineError as exc:
                # The provider response completed before output validation;
                # carry its paid fact through the row-error boundary.
                exc.accounting = accounting
                raise
            return self._shaped_result(
                spec,
                translation,
                detected_raw,
                cost=cost,
                cost_unknown=cost is None,
                model_calls=[fact],
            )

        if engine == "opus_mt":
            # In-process CTranslate2 local translate. The heavy
            # runtime lives in its own module (integrations/opus_mt.py); this
            # hook stays thin. Opus-MT is pair-based with NO language ID, so an
            # explicit source is required (the contract already rejects
            # empty/auto via allows_auto=False -- this is defence in depth).
            import asyncio

            from frisket.ops.integrations import opus_mt
            from frisket.engine.jobs import artifact_pull
            from frisket.ai.models import artifact_manifest

            source = self._source_language(spec)
            if not source:
                raise TranslateEngineError(
                    code="invalid_source",
                    message="Opus-MT requires an explicit source language.",
                )
            if not opus_mt.runtime_available():
                raise TranslateEngineError(
                    code="unavailable", message=opus_mt.REMEDIATION
                )
            try:
                src_code, tgt_code = opus_mt.resolve_pair_codes(
                    source, str(spec.get("target_language", "English"))
                )
            except ValueError as exc:
                raise TranslateEngineError(
                    code="invalid_target", message=str(exc)
                ) from exc
            cancel = self._cancel_signal(ctx)
            try:
                results = await asyncio.to_thread(
                    opus_mt.translate_texts, src_code, tgt_code, [text]
                )
            except opus_mt.OpusPairNotInstalled:
                # Lazy pull-on-first-use: the run queue has no host
                # affinity, so a row can land on any worker. If the pinned pair
                # is not on THIS worker yet, provision it worker-locally (no ack
                # -- it is manifest-gated) and retry once.
                try:
                    await asyncio.to_thread(
                        opus_mt.ensure_pair_installed,
                        src_code,
                        tgt_code,
                        should_cancel=cancel,
                    )
                    results = await asyncio.to_thread(
                        opus_mt.translate_texts, src_code, tgt_code, [text]
                    )
                except artifact_pull.ProvisionCancelled as exc:
                    # The run was cancelled mid-download. Retryable so
                    # backfill re-provisions + re-runs this row on resume.
                    raise TranslateEngineError(
                        code="cancelled",
                        message="Opus-MT pair provisioning was cancelled.",
                        retryable=True,
                    ) from exc
                except artifact_pull.ArtifactProvisionError as exc:
                    # Preserve the durable pull's integrity distinction. A
                    # checksum mismatch (tampered/corrupt upstream bytes) is
                    # TERMINAL and must NOT read as "not installed"; a transport
                    # / HTTP miss is retryable and genuinely "unreachable".
                    tampered = getattr(exc, "code", None) == "checksum_mismatch"
                    raise TranslateEngineError(
                        code="pair_tampered" if tampered else "pair_unreachable",
                        message=(
                            f"The {src_code}-{tgt_code} Opus-MT pair failed "
                            "integrity verification during provisioning "
                            "(checksum mismatch); the downloaded bytes were "
                            "discarded (corrupt or tampered upstream artifact)."
                            if tampered
                            else f"The {src_code}-{tgt_code} Opus-MT pair could "
                            f"not be provisioned ({exc})."
                        ),
                        retryable=not tampered,
                    ) from exc
                except Exception as exc:  # noqa: BLE001 -- unpinned / other miss
                    raise TranslateEngineError(
                        code="pair_unavailable",
                        message=(
                            f"The {src_code}-{tgt_code} Opus-MT pair is not "
                            "installed and could not be provisioned. Install it "
                            "to translate locally."
                        ),
                    ) from exc
            # Capture the actual pinned pair for the host receipt collector.
            pinned = artifact_manifest.lookup(f"opus-mt:{src_code}-{tgt_code}")
            extras = getattr(ctx, "extras", None)
            if pinned is not None and isinstance(extras, dict):
                extras.setdefault("artifact_provenance", []).append(
                    {
                        "ref": pinned.ref,
                        "manifest_version": pinned.manifest_version,
                        "license": pinned.license,
                        "license_url": pinned.license_url,
                        "composite_digest": pinned.composite_digest,
                        "source_url": pinned.source_url,
                    }
                )
            # Merge reconciliation: local pairs
            # are free (no cost meta) and opus_mt never detects — the explicit
            # source rides through as detected_raw so an API caller who set
            # save_detected_language still gets the honest (explicit) code.
            return self._shaped_result(
                spec,
                self._require_nonempty(results[0] if results else "", "opus_mt"),
                src_code,
            )

        if engine == "hy_mt2":
            # Experimental in-process Hy-MT2 GGUF.
            # The heavy llama.cpp runtime lives in its own module
            # (integrations/hy_mt2.py). Hy-MT2 auto-detects the source (the
            # card's prompt names only the target) and does NOT report a
            # detected language (detects=false) -- so no detected column and,
            # being local, no cost meta (free). Manifest-gated single artifact:
            # provision-on-miss then retry once.
            import asyncio

            from frisket.ops.integrations import hy_mt2
            from frisket.engine.jobs import artifact_pull
            from frisket.ai.models import artifact_manifest

            if not hy_mt2.runtime_available():
                raise TranslateEngineError(
                    code="unavailable", message=hy_mt2.REMEDIATION
                )
            target = str(spec.get("target_language", "English"))
            cancel = self._cancel_signal(ctx)
            try:
                results = await asyncio.to_thread(
                    hy_mt2.translate_texts, target, [text]
                )
            except hy_mt2.HyMt2NotInstalled:
                try:
                    await asyncio.to_thread(
                        hy_mt2.ensure_installed, should_cancel=cancel
                    )
                    results = await asyncio.to_thread(
                        hy_mt2.translate_texts, target, [text]
                    )
                except artifact_pull.ProvisionCancelled as exc:
                    # Cancelled mid-download -> retryable, backfill re-runs it.
                    raise TranslateEngineError(
                        code="cancelled",
                        message="Hy-MT2 model provisioning was cancelled.",
                        retryable=True,
                    ) from exc
                except artifact_pull.ArtifactProvisionError as exc:
                    # Preserve the integrity distinction (see opus_mt above).
                    tampered = getattr(exc, "code", None) == "checksum_mismatch"
                    raise TranslateEngineError(
                        code="model_tampered" if tampered else "model_unreachable",
                        message=(
                            "The Hy-MT2 model failed integrity verification "
                            "during provisioning (checksum mismatch); the "
                            "downloaded bytes were discarded (corrupt or "
                            "tampered upstream artifact)."
                            if tampered
                            else f"The Hy-MT2 model could not be provisioned ({exc})."
                        ),
                        retryable=not tampered,
                    ) from exc
                except Exception as exc:  # noqa: BLE001 -- unpinned / other miss
                    raise TranslateEngineError(
                        code="model_unavailable",
                        message=(
                            "The Hy-MT2 model is not installed and could not be "
                            "provisioned. Install it to translate locally."
                        ),
                    ) from exc
            # Capture the actual pinned model for the host receipt collector.
            pinned = artifact_manifest.hy_mt2_artifact()
            extras = getattr(ctx, "extras", None)
            if pinned is not None and isinstance(extras, dict):
                extras.setdefault("artifact_provenance", []).append(
                    {
                        "ref": pinned.ref,
                        "manifest_version": pinned.manifest_version,
                        "license": pinned.license,
                        "license_url": pinned.license_url,
                        "composite_digest": pinned.composite_digest,
                        "source_url": pinned.source_url,
                    }
                )
            # Local/free, no detection: detected_raw=None -> no detected column.
            return self._shaped_result(
                spec,
                self._require_nonempty(results[0] if results else "", "hy_mt2"),
                None,
            )

        raise RuntimeError(
            f"map.translate engine={engine!r} is not yet available in this build."
        )
