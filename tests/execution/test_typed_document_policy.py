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
from frisket.engine.runner.validation import NetworkDisabled, assert_network_policy
from frisket.engine.store import Project
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
    tmp_path, custom, engine
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
    project = Project.create(tmp_path / "policy.frisket")
    try:
        project.set_network_policy(mode="off")
        if engine == "datalab":
            with pytest.raises(NetworkDisabled):
                assert_network_policy(project, plan.program, spec)
        else:
            assert_network_policy(project, plan.program, spec)
    finally:
        project.close()
