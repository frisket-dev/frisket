"""Demo requests retain engine intent and placement through the typed boundary."""

import httpx
import pytest

from frisket.actions.system import typed_action_for_request
from scripts.e2e import seed_demo


@pytest.mark.parametrize(
    "seed", [seed_demo.seed_stories, seed_demo.seed_tariff, seed_demo.seed_feature_tour]
)
def test_demo_classification_preserves_model_and_companion_columns(seed, monkeypatch):
    requests = []
    monkeypatch.setattr(
        seed_demo,
        "run",
        lambda _client, _pid, request, _label: requests.append(request),
    )

    def handle(request):
        if request.url.path == "/api/projects":
            return httpx.Response(200, json={"id": "demo"})
        assert request.url.path == "/api/projects/demo/import/csv"
        return httpx.Response(200, json={"sheet_id": 17})

    with httpx.Client(
        base_url="http://frisket.test", transport=httpx.MockTransport(handle)
    ) as client:
        seed(client)

    classification = next(r for r in requests if r["action_id"] == "map.classify")
    bound = typed_action_for_request(classification)
    assert bound.params.engine.root == "llm"
    assert bound.params.model.root == seed_demo.MODEL
    assert bound.request.scope.sheet_id == 17
    assert bound.params.include_justification is True
    output_names = {field.key for field in bound.output_fields}
    for field in bound.params.fields:
        assert field.name in output_names
        assert f"{field.name}_justification" in output_names

    if seed is seed_demo.seed_feature_tour:
        summary = next(r for r in requests if r["action_id"] == "reduce.group_summary")
        bound_summary = typed_action_for_request(summary)
        assert bound_summary.request.sheet_name == "By verdict"
        assert bound_summary.request.scope.sheet_id == 17
        assert bound_summary.params.group_by.name == "follow_up"


def test_demo_sheet_destination_is_part_of_idempotency_identity():
    def request(name, sheet_id=17):
        return seed_demo.typed_action_request(
            "reduce.group_summary",
            sheet_id=sheet_id,
            sheet_name=name,
            params={
                "source": ["story"],
                "model": seed_demo.MODEL,
                "instruction": "Summarize these stories.",
            },
        )

    original = request("Summary")
    assert request("Summary") == original
    assert request("Other")["idempotency_key"] != original["idempotency_key"]
    assert request("Summary", 18)["idempotency_key"] != original["idempotency_key"]
    assert typed_action_for_request(original).request.sheet_name == "Summary"
