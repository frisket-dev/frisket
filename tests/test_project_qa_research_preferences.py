"""Research authorization is part of the immutable submitted question."""

import pytest

from frisket.engine.store import Project
from frisket.engine.store.project_qa import ProjectQAConflictError, ProjectQAStore


def test_research_settings_persist_and_cannot_change_on_request_replay(tmp_path):
    project = Project.create(tmp_path / "research.frisket", name="Research")
    try:
        store = ProjectQAStore(project)
        settings = {
            "write_mode": "ask_overwrite",
            "budget_usd": "1.25",
            "max_turns": None,
            "skills": ["document-research"],
        }
        thread = store.create_thread(title="Research", research=settings)
        assert store.get_thread(thread["id"])["research"] == settings
        turn = store.submit_turn(
            thread["id"], request_id="one", question="Analyze", research=settings
        )
        assert turn["research"] == settings
        assert (
            store.submit_turn(
                thread["id"], request_id="one", question="Analyze", research=settings
            )["id"]
            == turn["id"]
        )
        with pytest.raises(ProjectQAConflictError, match="different content"):
            store.submit_turn(
                thread["id"],
                request_id="one",
                question="Analyze",
                research={**settings, "write_mode": "full_access"},
            )
        changed = store.update_thread(
            thread["id"], expected_revision=thread["revision"], research=None
        )
        assert changed["research"] is None
        assert store.get_turn(turn["id"])["research"] == settings
    finally:
        project.close()
