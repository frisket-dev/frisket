"""NER's published list must remain usable by the Mentions surface."""

from frisket.engine.executor.actions import run_action_spec
from frisket.engine.store import Project
from frisket.querysets import is_entity_mentions_column


def test_typed_ner_publishes_a_recognized_mentions_column(tmp_path, monkeypatch):
    from frisket.ops import spacy_ner

    monkeypatch.setattr(spacy_ner, "spacy_available", lambda: (True, None))
    monkeypatch.setattr(
        spacy_ner,
        "run_spacy_ner_default",
        lambda *args, **kwargs: [
            [{"text": "Alice", "type": "person", "start": 0, "end": 5, "score": None}]
        ],
    )
    project = Project.create(tmp_path / "mentions.frisket")
    try:
        sheet = project.add_sheet("People")
        column = project.add_column(sheet, "text", type="text")
        project.add_rows(sheet, [{"text": "Alice"}], {"text": column})
        result = run_action_spec(
            project,
            {
                "action_id": "map.ner",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {"source": ["text"], "labels": ["person"], "engine": "spacy"},
                "output_names": {"entities": "mentions"},
                "idempotency_key": "ner-mentions",
            },
            project_id="mentions",
        )
        assert result.status == "completed", result.errors
        output = next(c for c in project.columns(sheet) if c["name"] == "mentions")
        assert output["semantic_type"] == "entity_mentions"
        assert is_entity_mentions_column(output)
    finally:
        project.close()
