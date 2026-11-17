"""Public commands do not reopen the removed generic lifecycle write seam."""

from frisket.engine.store.project_qa_research import ProjectQAResearchStore


def test_research_store_exposes_no_generic_lifecycle_patch_or_independent_finish() -> (
    None
):
    for retired in ("update", "finish", "interrupt_abandoned"):
        assert not hasattr(ProjectQAResearchStore, retired), (
            f"{retired} bypasses the parent turn lifecycle owner; add a named owner command instead"
        )
