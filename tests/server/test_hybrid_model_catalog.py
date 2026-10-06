from frisket.execution.credential_use import CredentialUseContext
from frisket.execution.definitions import StaticExecutionTargetProvider
from frisket.execution.price_book import PlatformMetered
from frisket.execution.provider import CompositionFacts, ExecutionComposition
from frisket.server.action_catalog_hints import (
    project_action_catalog_launcher_hints,
)


ROUTED_HYBRID_MODEL_ACTIONS = frozenset(
    {"map.translate", "map.classify", "map.extract"}
)
HYBRID_MODEL_ACTIONS = ROUTED_HYBRID_MODEL_ACTIONS | {"map.ner"}


def _platform_composition_without_execution_offers() -> ExecutionComposition:
    return ExecutionComposition(
        facts=CompositionFacts(
            edition="hosted",
            org_id="org-hybrid-catalog",
            funding=PlatformMetered(),
        ),
        provider=StaticExecutionTargetProvider(env={}),
        credential_use_context=CredentialUseContext(cost_posture="platform_metered"),
    )


def test_platform_catalog_keeps_model_branch_of_hybrid_actions() -> None:
    hints = project_action_catalog_launcher_hints(
        {},
        org_provider_keys={"openai": "fixture-key"},
        execution_composition=_platform_composition_without_execution_offers(),
        action_kinds=HYBRID_MODEL_ACTIONS,
    )

    routed_engines = {
        action_id: [engine["id"] for engine in hints[action_id].get("engines", [])]
        for action_id in ROUTED_HYBRID_MODEL_ACTIONS
    }
    assert routed_engines == {
        action_id: ["llm"] for action_id in ROUTED_HYBRID_MODEL_ACTIONS
    }

    ner_engines = [engine["id"] for engine in hints["map.ner"]["engines"]]
    assert "llm" in ner_engines


def test_platform_model_branch_does_not_admit_an_unoffered_routed_action() -> None:
    hints = project_action_catalog_launcher_hints(
        {},
        org_provider_keys={"openai": "fixture-key"},
        execution_composition=_platform_composition_without_execution_offers(),
        action_kinds=frozenset({"media.transcribe"}),
    )

    assert "engines" not in hints["media.transcribe"]
