from __future__ import annotations

import pytest

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.document_types import (
    ConvertedDocument,
    DocumentColumn,
    DocumentConverter,
)
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    EngineRef,
    Row,
    RowResult,
    SheetRows,
)
from frisket.engine.executor.map_rows_action import _typed_map_rows_plan
from frisket.engine.runner.network_policy import remote_capability_for_spec
from frisket.ai.llm import ModelRouter
from frisket.engine.runner.validation import ServiceRefused, assert_allowed_services
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.project_service_access import SelectedRoute
from frisket.execution.targets import CAPABILITY_TO_MARKDOWN


class CustomDocumentParams(ActionParams):
    document: DocumentColumn
    converter: EngineRef[DocumentConverter]


async def custom_document(
    params: CustomDocumentParams, row: Row, documents: DocumentConverter
) -> RowResult[ConvertedDocument]:
    return RowResult(output=await documents.convert(row, params.document))


CUSTOM = ActionRegistry(
    [
        ActionNamespace(
            "custom",
            actions=[
                action(
                    name="document",
                    title="Custom document",
                    description="Convert a document.",
                    category=ActionCategory.EXTRACT,
                    run=map_rows(
                        custom_document, active_outputs=lambda params: ("markdown",)
                    ),
                )
            ],
        )
    ]
)


@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("engine", ["markitdown", "docling", "datalab"])
def test_document_network_policy_follows_capability_not_builtin_id(
    make_project, custom, engine
):
    action_id = "custom.document" if custom else "media.to_markdown"
    registry = CUSTOM if custom else ACTION_REGISTRY
    request = ActionRequest(
        action_id=action_id,
        scope=SheetRows(sheet_id=1),
        params=(
            {"document": "source", "converter": engine}
            if custom
            else {"source": "source", "engine": engine}
        ),
        idempotency_key="document-policy",
    )
    plan = _typed_map_rows_plan(
        BoundTypedActionRequest.bind(registry.get(action_id), request)
    )
    spec = plan.spec_dict()
    assert spec["engine"] == engine
    assert plan.program.execution_capability == CAPABILITY_TO_MARKDOWN
    # Request data cannot relabel the capability or the engine's remoteness.
    spec["execution_capability"] = (
        "local" if engine == "datalab" else CAPABILITY_TO_MARKDOWN
    )
    spec["capabilities"] = [] if engine == "datalab" else ["external:datalab"]
    remote = remote_capability_for_spec(plan.program, spec)
    assert remote == ("engine:datalab" if engine == "datalab" else None)
    project = make_project("policy")
    project.set_allowed_services("approved_services", confirm_allowed_services=None)
    if engine == "datalab":
        with pytest.raises(ServiceRefused):
            assert_allowed_services(
                project, plan.program, spec, router=_router(), selected_route=None
            )
    else:
        assert_allowed_services(
            project, plan.program, spec, router=_router(), selected_route=None
        )


def _router() -> ModelRouter:
    return ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, use_env_keys=False)


def test_network_policy_uses_selected_route_egress(make_project):
    request = ActionRequest(
        action_id="media.to_markdown",
        scope=SheetRows(sheet_id=1),
        params={"source": "source", "engine": "docling"},
        idempotency_key="selected-route-policy",
    )
    plan = _typed_map_rows_plan(
        BoundTypedActionRequest.bind(ACTION_REGISTRY.get(request.action_id), request)
    )
    spec = plan.spec_dict()
    project = make_project("selected-route")
    project.set_allowed_services("approved_services", confirm_allowed_services=None)
    assert_allowed_services(
        project,
        plan.program,
        spec,
        router=_router(),
        selected_route=SelectedRoute(
            "models-gateway", "operator_lan", "http://127.0.0.1:9000"
        ),
    )
    with pytest.raises(ServiceRefused, match="Frisket Cloud"):
        assert_allowed_services(
            project,
            plan.program,
            spec,
            router=_router(),
            selected_route=SelectedRoute("frisket-cloud", "frisket_shared"),
        )
