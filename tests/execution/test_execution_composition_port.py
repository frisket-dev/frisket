"""Closure and request-scoping proofs for the downstream composition port."""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

import dis
import importlib.util
import pkgutil
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from types import CodeType

import frisket
import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.executor.actions import (
    _composition_bound_map_runner_factory,
    _default_map_runner_factory,
)
from frisket.engine.runner import validation
from frisket.engine.runner.confirmation_context import quoted_usd
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell
from frisket.execution.credential_use import CredentialOwner, CredentialUseContext
from frisket.execution.attempt_authority import AttemptAuthority, admit_routed
from frisket.execution.commercial import (
    CommercialOffering,
    CommercialOfferingMatch,
    CommercialPresentation,
    CommercialQuoteRounding,
    CommercialQuoteTerms,
    CommercialSettlementTerms,
)
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.execution.price_book import ByokZero, PlatformMetered
from frisket.execution.pricing_policy import QuoteFacts, RatedQuote
from frisket.execution.provider import (
    CompositionFacts,
    ExecutionComposition,
    ExecutionCompositionContext,
)
from frisket.execution.resolve_for_action import resolve_for_action
from frisket.execution.resolver import Refusal, ResolvedExecution
from tests.execution.typed_transcription_helpers import (
    transcription_spec,
    transcription_program,
)
from frisket.server.workspace import Workspace


def test_request_context_is_typed_detached_and_passed_by_identity(tmp_path) -> None:
    project = Project.create(tmp_path / "context.frisket")
    snapshot = {"funding": {"reservation": 7}}
    context = ExecutionCompositionContext(
        storage_key=None,
        run_id=None,
        trusted_job_org_id=ExecutionCompositionContext.direct().trusted_job_org_id,
        edition_snapshot=snapshot,
    )
    snapshot["funding"]["reservation"] = 99
    assert context.edition_snapshot == {"funding": {"reservation": 7}}

    observed = []

    def factory(_project, _router, received):
        observed.append(received)
        return ExecutionComposition(
            facts=CompositionFacts(),
            provider=StaticExecutionTargetProvider(secrets=project, router=None),
            credential_use_context=CredentialUseContext.open(),
        )

    workspace = object.__new__(Workspace)
    workspace._execution_composition_factory = factory
    request = SimpleNamespace(
        state=SimpleNamespace(execution_composition_context=context)
    )
    received = Workspace.execution_composition_context_for(request)
    workspace.execution_composition_for(project, ModelRouter(), received)
    assert observed == [context]
    assert observed[0] is context


def test_request_context_refuses_untyped_state() -> None:
    request = SimpleNamespace(
        state=SimpleNamespace(execution_composition_context={"edition": "hosted"})
    )
    with pytest.raises(TypeError, match="ExecutionCompositionContext"):
        Workspace.execution_composition_context_for(request)


def _nested_code_objects(code: CodeType):
    yield code
    for value in code.co_consts:
        if isinstance(value, CodeType):
            yield from _nested_code_objects(value)


@cache
def _production_code_objects() -> tuple[tuple[str, CodeType], ...]:
    found: list[tuple[str, CodeType]] = []
    for module in pkgutil.walk_packages(frisket.__path__, "frisket."):
        spec = importlib.util.find_spec(module.name)
        loader = None if spec is None else spec.loader
        get_code = getattr(loader, "get_code", None)
        if get_code is None:
            continue
        code = get_code(module.name)
        if code is not None:
            found.extend((module.name, nested) for nested in _nested_code_objects(code))
    return tuple(found)


def _production_calls_to(
    target: str,
) -> list[tuple[str, str, int | None, frozenset[str]]]:
    """Locate named calls in import-loader code objects, without source reads."""
    found: list[tuple[str, str, int | None, frozenset[str]]] = []
    callable_loads = {
        "LOAD_ATTR",
        "LOAD_DEREF",
        "LOAD_FAST",
        "LOAD_GLOBAL",
        "LOAD_METHOD",
        "LOAD_NAME",
    }
    for module_name, code in _production_code_objects():
        instructions = list(dis.get_instructions(code))
        for index, instruction in enumerate(instructions):
            if instruction.opname not in callable_loads or instruction.argval != target:
                continue
            start = (
                instruction.positions.lineno,
                instruction.positions.col_offset,
            )
            keywords: frozenset[str] = frozenset()
            for candidate in instructions[index + 1 :]:
                candidate_start = (
                    candidate.positions.lineno,
                    candidate.positions.col_offset,
                )
                if candidate_start != start:
                    continue
                if candidate.opname == "KW_NAMES":
                    keywords = frozenset(candidate.argval or ())
                elif candidate.opname == "CALL":
                    found.append(
                        (
                            module_name,
                            code.co_qualname,
                            instruction.positions.lineno,
                            keywords,
                        )
                    )
                    break
    return found


def test_composition_facts_have_one_production_constructor() -> None:
    """D-01: production cannot grow a second, partly-defaulted fact mint."""
    calls = _production_calls_to("CompositionFacts")
    assert [(module, owner) for module, owner, _line, _keys in calls] == [
        ("frisket.execution.provider", "open_execution_composition")
    ], calls


def test_every_production_map_runner_consumes_the_composition_carrier() -> None:
    """All construction surfaces must carry composition, including new ones."""
    found = _production_calls_to("MapRunner")
    # The retired semantic recipe used a separate constructor; its typed host
    # now uses the existing composition-bound factory.
    assert len(found) == 8, found  # Installed rows share the builtin runner host.
    missing = [
        (module, owner, line)
        for module, owner, line, keys in found
        if "execution_composition" not in keys
    ]
    assert not missing, f"MapRunner construction drops execution composition: {missing}"


def test_every_production_attempt_authority_consumes_the_full_composition() -> None:
    """Fresh prepared bindings and resume share targets, offers, and funding."""
    found = {
        name: _production_calls_to(name)
        for name in ("AttemptAuthority", "admit_routed", "build_attempt_authority")
    }
    assert {name: len(calls) for name, calls in found.items()} == {
        "AttemptAuthority": 5,  # Ordinary and uploaded previews own attempts.
        "admit_routed": 2,
        "build_attempt_authority": 5,
    }, found
    missing = [
        (name, module, owner, line)
        for name, calls in found.items()
        for module, owner, line, keys in calls
        if "composition" not in keys
    ]
    assert not missing, f"attempt authority would drop composition F: {missing}"


def test_routed_authority_has_no_implicit_open_composition_fallback() -> None:
    """The old one-argument forms are unrepresentable at every public seam."""
    from frisket.engine.jobs.runs import build_attempt_authority

    with pytest.raises(TypeError, match="composition"):
        AttemptAuthority(object())  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="composition"):
        admit_routed(object(), 1, {})  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="composition"):
        build_attempt_authority(object())  # type: ignore[call-arg]


def test_two_requests_keep_effective_provider_funding_and_admission_together(
    tmp_path: Path, monkeypatch
) -> None:
    """S != F regression: no storage/env fallback and no cached fact bleed."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    project = Project.create(tmp_path / "composition.frisket")
    sheet_id = project.add_sheet("Audio")
    column_id = project.add_column(sheet_id, "audio", type="audio")
    blob = project.add_blob(
        b"RIFFxxxxWAVEfmt ",
        filename="a.wav",
        mime="audio/wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 60.0, "kind": "audio"}
        ),
    )
    project.add_rows(
        sheet_id,
        [{"audio": media_cell(blob, mime="audio/wav", filename="a.wav")}],
        {"audio": column_id},
    )
    assert project.provider_model_keys() == {}

    org_router = ModelRouter(
        keys={"openai": "org-key"},
        key_sources={"openai": "org_byok"},
        use_env_keys=False,
    )
    platform_router = ModelRouter(
        keys={"openai": "platform-key"},
        key_sources={"openai": "platform_key"},
        use_env_keys=False,
    )
    calls: list[ModelRouter] = []

    def factory(
        _project: Project,
        router: ModelRouter,
        _context: ExecutionCompositionContext,
    ) -> ExecutionComposition:
        calls.append(router)
        source = router.configured_key_sources()["openai"]
        if source == "org_byok":
            funding = ByokZero()
            owner = CredentialOwner.organization("org-a")
            context = CredentialUseContext(
                cost_posture="org_key",
                consented_owner=owner,
                selected_owner=owner,
            )
        else:
            funding = PlatformMetered()
            owner = CredentialOwner.deployment("deployment-a")
            context = CredentialUseContext(
                cost_posture="platform_metered",
                consented_owner=owner,
                selected_owner=owner,
                deployment_owner=owner,
            )
        provider = StaticExecutionTargetProvider(
            secrets=_project,
            router=router,
        )
        offerings = ()
        if isinstance(funding, PlatformMetered):
            offerings = (
                CommercialOffering(
                    match=CommercialOfferingMatch(
                        target_id="remote-api:openai",
                        capability="transcribe",
                        engine="openai/whisper-1",
                    ),
                    quote=CommercialQuoteTerms(
                        pricing_key="test.synthetic.transcription.audio_minute",
                        terms_version="test.synthetic.terms.v1",
                        unit_rate="0.02",
                        quantity_unit="audio_minute",
                        rounding=CommercialQuoteRounding("half_even", 6),
                    ),
                    settlement=CommercialSettlementTerms(
                        meter_key="audio_seconds",
                        meter_units_per_quantity_unit="60",
                        ceiling_mode="consented_quantity",
                        row_settlement_mode="all_metered",
                    ),
                    charge_authority="test.synthetic.authority",
                    presentation=CommercialPresentation(
                        venue_label="Synthetic venue",
                        billing_label="Synthetic usage billing",
                    ),
                ),
            )
        return ExecutionComposition(
            facts=CompositionFacts(
                edition="hosted",
                org_id="org-a",
                funding=funding,
            ),
            provider=provider,
            credential_use_context=context,
            offerings=offerings,
        )

    workspace = object.__new__(Workspace)
    workspace._execution_composition_factory = factory
    direct_context = ExecutionCompositionContext.direct()
    org_composition = workspace.execution_composition_for(
        project, org_router, direct_context
    )
    platform_composition = workspace.execution_composition_for(
        project, platform_router, direct_context
    )
    assert calls == [org_router, platform_router]
    assert org_composition is not platform_composition

    spec = transcription_spec(sheet_id, source="audio", engine="openai/whisper-1")
    recipe = transcription_program(spec)
    org_resolution = resolve_for_action(
        project,
        spec,
        recipe,
        composition=org_composition,
    )
    platform_resolution = resolve_for_action(
        project,
        spec,
        recipe,
        composition=platform_composition,
    )
    assert isinstance(org_resolution, ResolvedExecution)
    assert isinstance(platform_resolution, ResolvedExecution)
    assert not isinstance(org_resolution, Refusal)
    assert org_resolution.resolution.facts.cost_posture == "org_key"
    assert platform_resolution.resolution.facts.cost_posture == "platform_metered"

    class SyntheticOfferingPolicy:
        """Test-only downstream answer over the base pricing-policy port."""

        policy_id = "test.synthetic-offering.v1"

        def rate(self, facts: QuoteFacts) -> RatedQuote:
            offered = facts.pricing_key == "test.synthetic.transcription.audio_minute"
            return RatedQuote(
                billed_cost=(facts.provider_cost or 0) if offered else 0,
                provider_cost=facts.provider_cost,
                lane="published_sku" if offered else "byok",
                policy_id=self.policy_id,
            )

    policy = SyntheticOfferingPolicy()

    org_estimate = validation.estimate_run(
        project,
        spec,
        composition=org_composition,
        program=recipe,
        pricing_policy=policy,
    )
    platform_estimate = validation.estimate_run(
        project,
        spec,
        composition=platform_composition,
        program=recipe,
        pricing_policy=policy,
    )
    assert quoted_usd(org_estimate) == 0
    assert quoted_usd(platform_estimate) > 0

    # The authority is used by both fresh admission (with the prepared
    # binding) and resume admission (provider re-deref).  Both must hold the
    # very same provider that produced resolution, not rebuild from project S.
    org_runner = _default_map_runner_factory(
        project,
        org_router,
        execution_composition=org_composition,
    )
    platform_runner = _default_map_runner_factory(
        project,
        platform_router,
        execution_composition=platform_composition,
    )
    assert org_runner.execution_composition is org_composition
    assert org_runner.authority.composition is org_composition
    assert platform_runner.execution_composition is platform_composition
    assert platform_runner.authority.composition is platform_composition

    # The custom-factory injection seat must bind the resume-time authority,
    # too.  Otherwise cloud could inject F for resolution while a resumed
    # attempt silently fell back to the factory's stale provider S.
    class CustomRunner:
        execution_composition = None

        def __init__(self) -> None:
            self.authority = AttemptAuthority(
                project,
                composition=platform_composition,
            )

    custom_factory = _composition_bound_map_runner_factory(
        lambda _project, _router: CustomRunner(),
        org_composition,
    )
    custom_runner = custom_factory(project, org_router)
    assert custom_runner.execution_composition is org_composition
    assert custom_runner.authority.composition is org_composition
    project.close()
