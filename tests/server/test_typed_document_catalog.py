"""Document engine choices belong to the capability, not an action's name."""

from pydantic import BaseModel

from frisket.actions.core import (
    ActionCategory,
    ActionNamespace,
    ActionRegistry,
    action,
    map_rows,
)
from frisket.actions.document_types import (
    DocumentColumn,
    DocumentConverter,
)
from frisket.actions.types import ActionParams, EngineRef, Row, RowResult
from frisket.server.action_catalog_hints import project_action_catalog_launcher_hints


class ReadDocumentParams(ActionParams):
    manuscript: DocumentColumn
    converter: EngineRef[DocumentConverter] = EngineRef[DocumentConverter]("docling")


class ReadDocumentOutput(BaseModel):
    text: str


async def read_document(
    params: ReadDocumentParams, row: Row, reader: DocumentConverter
) -> RowResult[ReadDocumentOutput]:
    document = await reader.convert(row, params.manuscript)
    return RowResult(output=ReadDocumentOutput(text=document.markdown))


def test_custom_document_action_receives_the_same_engine_choices(monkeypatch):
    import frisket.actions.registry as registry_module

    sidecar = {"configured": False, "available": False, "engines": [], "error": None}
    expected = project_action_catalog_launcher_hints(sidecar)["media.to_markdown"][
        "engines"
    ]
    registry = ActionRegistry(
        (
            ActionNamespace(
                "example",
                actions=(
                    action(
                        name="read_manuscript",
                        title="Read manuscript",
                        description="Convert a manuscript to text.",
                        category=ActionCategory.EXTRACT,
                        run=map_rows(read_document),
                    ),
                ),
            ),
        )
    )
    monkeypatch.setattr(registry_module, "ACTION_REGISTRY", registry)

    hints = project_action_catalog_launcher_hints(sidecar)["example.read_manuscript"]

    assert hints["engines"] == expected
    assert {engine["id"] for engine in hints["engines"]} == {
        "markitdown",
        "trafilatura_html",
        "docling",
        "chandra",
        "datalab",
    }
