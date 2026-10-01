from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.workspaces import ReadOnlyWorkspace, WorkspaceReadOnlyError
from pydantic_ai_harness import Skills
from pydantic_ai_harness.compaction import SummarizingCompaction, TieredCompaction

from frisket.server.services.project_qa_capabilities import (
    ProjectQACapabilityError,
    compose_project_qa_capabilities,
)


class _Model:
    def __init__(self, context_window: int | None):
        self.context_window = context_window


def _skill(name: str = "source-check") -> dict[str, object]:
    return {
        "name": name,
        "revision": 7,
        "content": (
            "---\n"
            f"name: {name}\n"
            "description: Check claims against exact source material.\n"
            "---\n\n"
            "Inspect the cited passage before accepting a claim.\n"
        ),
    }


@pytest.mark.anyio
async def test_composition_materializes_a_read_only_progressive_skill_library() -> None:
    library_root: Path | None = None

    with compose_project_qa_capabilities(_Model(1_000_000), [_skill()]) as capabilities:
        assert [type(capability) for capability in capabilities] == [
            Skills,
            TieredCompaction,
        ]
        skills = capabilities[0]
        assert isinstance(skills, Skills)
        assert isinstance(skills.workspace, ReadOnlyWorkspace)
        assert skills.workspace.read_only is True
        assert skills.workspace.ref is not None
        library_root = Path(skills.workspace.ref.id)
        assert (library_root / "skills/source-check/SKILL.md").read_text() == _skill()[
            "content"
        ]

        with pytest.raises(WorkspaceReadOnlyError):
            await skills.workspace.write_text("skills/source-check/SKILL.md", "changed")
        with pytest.raises(WorkspaceReadOnlyError):
            await skills.workspace.run(["pwd"])

        compaction = capabilities[1]
        assert isinstance(compaction, TieredCompaction)
        assert compaction.target_fraction == 0.75
        [summary] = compaction.tiers
        assert isinstance(summary, SummarizingCompaction)
        assert summary.model is None
        assert summary.receipts is True
        assert "source and citation IDs" in summary.summary_prompt
        assert "pending action and tool-call references" in summary.summary_prompt

    assert library_root is not None
    assert not library_root.exists()


def test_composition_rejects_unsafe_or_ambiguous_skill_snapshots() -> None:
    with pytest.raises(ProjectQACapabilityError, match="canonical"):
        with compose_project_qa_capabilities(_Model(1_000_000), [_skill("../escape")]):
            pass

    duplicate = _skill()
    with pytest.raises(ProjectQACapabilityError, match="duplicate"):
        with compose_project_qa_capabilities(
            _Model(1_000_000), [duplicate, dict(duplicate)]
        ):
            pass

    invalid_revision = _skill()
    invalid_revision["revision"] = "7"
    with pytest.raises(ProjectQACapabilityError, match="revision"):
        with compose_project_qa_capabilities(_Model(1_000_000), [invalid_revision]):
            pass


def test_compaction_requires_known_capacity_or_explicit_disable() -> None:
    with pytest.raises(ProjectQACapabilityError, match="context capacity"):
        with compose_project_qa_capabilities(_Model(None), []):
            pass

    with compose_project_qa_capabilities(
        _Model(None), [_skill()], enable_compaction=False
    ) as capabilities:
        assert [type(capability) for capability in capabilities] == [Skills]
