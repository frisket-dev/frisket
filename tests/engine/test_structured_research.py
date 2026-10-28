"""Structured research uses the ordinary research loop and row publication."""

from __future__ import annotations

import json
from types import MappingProxyType

import pytest
from pydantic import ValidationError

from executor_harness import run_action_with_confirmation
from frisket.actions.core import RegisteredAction
from frisket.actions.extraction_types import ExtractField, extraction_output_fields
from frisket.actions.registry import ACTION_REGISTRY
from frisket.actions.row_research import EXTRACT, StructuredResearchParams
from frisket.ai.llm import LLMResponse, ModelRouter
from frisket.engine.executor import research_read
from frisket.engine.store import Project


FIELDS = [
    {"name": "headquarters", "type": "text", "description": "City of the headquarters"},
    {"name": "employee_count", "type": "integer"},
    {"name": "public_company", "type": "boolean"},
]


def params(**updates):
    return StructuredResearchParams(
        **{
            "source": ["company"],
            "question": {"text": "Research {{company}}"},
            "model": "anthropic/claude-haiku-4-5",
            "fields": FIELDS,
            **updates,
        }
    )


def test_structured_research_output_types_and_optional_companions():
    fields = EXTRACT.run.resolve_output_fields(params(include_confidence=True))
    assert [(field.key, field.column_type) for field in fields] == [
        ("headquarters", "text"),
        ("employee_count", "integer"),
        ("public_company", "boolean"),
        ("headquarters_confidence", "number"),
        ("sources", "json"),
    ]
    without = EXTRACT.run.resolve_output_fields(params(include_sources=False))
    assert [field.key for field in without] == [field["name"] for field in FIELDS]


def test_structured_research_refuses_source_and_generated_confidence_collisions():
    with pytest.raises(ValidationError, match="Rename the sources field"):
        params(fields=[{"name": "sources", "type": "text"}])
    assert (
        params(fields=[{"name": "sources", "type": "text"}], include_sources=False)
        .fields[0]
        .name
        == "sources"
    )
    with pytest.raises(ValueError, match="invalid_params"):
        extraction_output_fields(
            [
                ExtractField(name="value", type="text"),
                ExtractField(name="value_confidence", type="number"),
            ],
            include_confidence=True,
        )


class ResearchAdapter:
    def __init__(self, *, invalid=False):
        self.requests = []
        self.invalid = invalid
        self.final_calls = 0

    async def complete(self, request, _client):
        self.requests.append(request)
        if request.tools:
            if len(self.requests) == 1:
                return self.reply(
                    request,
                    tool_calls=[
                        {
                            "name": "search",
                            "args": {"query": "Acme annual report"},
                            "id": "search1",
                        }
                    ],
                )
            return self.reply(
                request,
                content="Acme is headquartered in Paris. It employs 12 people. Its listing status is unknown.",
            )
        assert request.schema is not None
        self.final_calls += 1
        # Exercise ordinary schema repair without redoing the research.
        data = {"headquarters": "Paris"}
        if self.final_calls > 1 and not self.invalid:
            data.update(
                employee_count=12, public_company=None, headquarters_confidence=0.9
            )
        return self.reply(request, content=json.dumps(data), data=data)

    @staticmethod
    def reply(request, *, content=None, data=None, tool_calls=None):
        return LLMResponse(
            content=content,
            data=data,
            tool_calls=tool_calls,
            tokens_in=10,
            tokens_out=5,
            cost=0.001,
            model=request.model,
        )


@pytest.mark.parametrize("invalid", [False, True])
@pytest.mark.parametrize("include_sources", [False, True])
def test_structured_research_runs_once_then_validates_and_accounts(
    tmp_path,
    monkeypatch,
    invalid,
    include_sources,
):
    # Exercise this declaration through the production host; the coordinator
    # registers it in the shared namespace as part of integration.
    monkeypatch.setattr(
        ACTION_REGISTRY,
        "_actions",
        MappingProxyType(
            {
                **ACTION_REGISTRY._actions,
                "research.extract": RegisteredAction("research.extract", EXTRACT),
            }
        ),
    )
    searches = []

    async def search(_bound, query):
        searches.append(query)
        return "Acme has 12 employees in Paris.", ["https://example.test/filing"]

    monkeypatch.setattr(research_read._BoundResearcher, "_search", search)
    adapter = ResearchAdapter(invalid=invalid)
    router = ModelRouter(keys={"anthropic": "test-key"}, cache=None, cache_mode="off")
    router._adapters["anthropic"] = adapter
    project = Project.create(tmp_path / "structured-research")
    try:
        sheet = project.add_sheet("Companies")
        column = project.add_column(sheet, "company", type="text")
        project.add_rows(sheet, [{"company": "Acme"}], {"company": column})
        request = {
            "action_id": "research.extract",
            "scope": {"kind": "sheet_rows", "sheet_id": sheet},
            "params": params(
                include_sources=include_sources, include_confidence=True
            ).model_dump(mode="json"),
            "idempotency_key": "structured-research-test",
        }
        result = run_action_with_confirmation(
            project, request, project_id="research", router=router
        )
        assert result.status == ("failed" if invalid else "completed"), result.errors
        assert len(searches) == 1
        assert len(adapter.requests) == 4
        assert "employee_count" in json.dumps(adapter.requests[0].messages)
        assert "Research findings" in json.dumps(adapter.requests[2].messages)
        assert "https://example.test/filing" in json.dumps(adapter.requests[2].messages)
        assert all(request.tools is None for request in adapter.requests[2:])
        columns = {column["name"]: column for column in project.columns(sheet)}
        assert ("sources" in columns) is include_sources
        if not invalid:
            assert list(
                project.get_values(sheet, columns["headquarters"]["id"]).values()
            ) == ["Paris"]
            assert list(
                project.get_values(sheet, columns["employee_count"]["id"]).values()
            ) == [12]
            assert list(
                project.get_values(sheet, columns["public_company"]["id"]).values()
            ) == [None]
            if include_sources:
                assert list(
                    project.get_values(sheet, columns["sources"]["id"]).values()
                ) == [["https://example.test/filing"]]
        else:
            assert (
                project.db.execute(
                    "SELECT count(*) FROM results WHERE error IS NOT NULL"
                ).fetchone()[0]
                >= 1
            )
        calls = project.db.execute(
            "SELECT provider_cost_usd FROM model_calls WHERE run_id=?", (result.run_id,)
        ).fetchall()
        assert len(calls) == 4
        assert sum(float(call["provider_cost_usd"]) for call in calls) == pytest.approx(
            0.004
        )
    finally:
        project.close()
