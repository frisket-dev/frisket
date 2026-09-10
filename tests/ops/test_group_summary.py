"""Group order, provider prompts and quotes share the actual admitted inputs."""

from types import SimpleNamespace

from frisket.ops.group_summary import (
    estimate_group_summary,
    group_summary_messages,
    group_summary_rows,
)


def test_grouping_keeps_order_contributors_and_selected_values():
    rows = {
        9: {"beat": "A", "story": "first", "ignored": "x"},
        3: {"beat": None, "story": "second"},
        7: {"beat": "A", "story": "last"},
    }
    groups = group_summary_rows(rows, source=["story"], group_by="beat")
    assert [group["name"] for group in groups] == ["A", "null"]
    assert groups[0]["source_row_ids"] == [9, 7]
    assert groups[0]["rows"] == [
        {"row_id": 9, "values": {"story": "first"}},
        {"row_id": 7, "values": {"story": "last"}},
    ]
    assert group_summary_rows(rows, source=["story"], group_by=None)[0][
        "source_row_ids"
    ] == [9, 3, 7]


def test_same_actual_messages_drive_quote_hash_even_when_price_is_unchanged(
    monkeypatch,
):
    monkeypatch.setattr(
        "frisket.ops.group_summary.model_pricing",
        lambda model: SimpleNamespace(
            price=(0.1, 0.1), cost_source="catalog", pricing_key=model
        ),
    )
    groups = group_summary_rows(
        {2: {"story": "First story"}}, source=["story"], group_by=None
    )
    messages = [group_summary_messages("Keep outliers", group) for group in groups]
    estimate = estimate_group_summary("test/model", messages)
    assert "Instruction: Keep outliers" in messages[0][1]["content"]
    assert '"row_id": 2' in messages[0][1]["content"]
    assert estimate["groups"] == 1
    assert estimate["pricing_key"] == "test/model"
    groups[0]["rows"][0]["values"]["story"] = "Other story"
    changed = estimate_group_summary(
        "test/model", [group_summary_messages("Keep outliers", groups[0])]
    )
    assert changed["cost"] == estimate["cost"]
    assert changed["resolved_prompt_hash"] != estimate["resolved_prompt_hash"]


def test_unknown_group_quote_never_claims_zero(monkeypatch):
    monkeypatch.setattr(
        "frisket.ops.group_summary.model_pricing", lambda _: SimpleNamespace(price=None)
    )
    groups = group_summary_rows({2: {"story": False}}, source=["story"], group_by=None)
    messages = [group_summary_messages("Summarize", group) for group in groups]
    estimate = estimate_group_summary("test/unpriced", messages)
    assert estimate["cost"] is None
    assert estimate["cost_source"] == "unknown"
    assert '"story": false' in messages[0][1]["content"]
