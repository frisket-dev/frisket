"""Bounded Harness capability composition for one Project Ask research run."""

from __future__ import annotations

import re
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Protocol

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.workspaces import (
    LocalWorkspaceBackend,
    ReadOnlyWorkspace,
    Workspace,
)
from pydantic_ai_harness import Skills
from pydantic_ai_harness.compaction import SummarizingCompaction, TieredCompaction


DEFAULT_COMPACTION_FRACTION = 0.75
_SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$", re.ASCII)
_SUMMARY_FOCUS = (
    "Preserve the user's research goal, exact source and citation IDs, and "
    "pending action and tool-call references needed to continue. Preserve only "
    "facts present in the conversation; do not invent identifiers or a separate notebook."
)


class ProjectQACapabilityError(ValueError):
    """The selected runtime inputs cannot form a safe capability set."""


class ContextSizedModel(Protocol):
    @property
    def context_window(self) -> int | None: ...


@dataclass(frozen=True)
class _SkillSnapshot:
    name: str
    content: str
    revision: int


def _freeze_skill_snapshots(
    snapshots: Sequence[Mapping[str, object]],
) -> tuple[_SkillSnapshot, ...]:
    frozen: list[_SkillSnapshot] = []
    seen: set[str] = set()
    for snapshot in snapshots:
        name = snapshot.get("name")
        content = snapshot.get("content")
        revision = snapshot.get("revision")
        if (
            not isinstance(name, str)
            or len(name) > 64
            or _SKILL_NAME.fullmatch(name) is None
        ):
            raise ProjectQACapabilityError(
                "Skill snapshot names must be canonical lowercase letters, digits, "
                "and single hyphens."
            )
        if name in seen:
            raise ProjectQACapabilityError(
                f"Skill snapshot name {name!r} is duplicate."
            )
        if not isinstance(content, str) or not content or "\x00" in content:
            raise ProjectQACapabilityError(
                f"Skill snapshot {name!r} must contain non-empty text."
            )
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ProjectQACapabilityError(
                f"Skill snapshot {name!r} must have a positive integer revision."
            )
        seen.add(name)
        frozen.append(_SkillSnapshot(name=name, content=content, revision=revision))
    return tuple(frozen)


def _compaction(
    model: ContextSizedModel, target_fraction: float
) -> TieredCompaction[Any]:
    capacity = model.context_window
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
        raise ProjectQACapabilityError(
            "The selected model has no known context capacity; compaction requires "
            "a real model profile capacity or must be explicitly disabled."
        )
    summary = SummarizingCompaction[Any](
        model=None,
        max_fraction=target_fraction,
        keep_messages=20,
        receipts=True,
        tool_return_max_chars=4_000,
    ).with_focus(_SUMMARY_FOCUS)
    return TieredCompaction[Any](
        tiers=[summary],
        target_fraction=target_fraction,
    )


def _skill_capability(root: Path, snapshots: tuple[_SkillSnapshot, ...]) -> Skills[Any]:
    library = root / "skills"
    library.mkdir()
    for snapshot in snapshots:
        skill_directory = library / snapshot.name
        skill_directory.mkdir()
        skill_file = skill_directory / "SKILL.md"
        skill_file.write_text(snapshot.content, encoding="utf-8")
        skill_file.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    backend = LocalWorkspaceBackend(root)
    workspace = ReadOnlyWorkspace(Workspace(backend))
    return Skills[Any]("skills", workspace=workspace)


@contextmanager
def compose_project_qa_capabilities(
    model: ContextSizedModel,
    skill_snapshots: Sequence[Mapping[str, object]],
    *,
    enable_compaction: bool = True,
    target_fraction: float = DEFAULT_COMPACTION_FRACTION,
) -> Iterator[list[AbstractCapability[Any]]]:
    """Yield capabilities bound to one immutable Project Ask run snapshot.

    The context must enclose the whole agent run. Its temporary skill library is
    deleted on exit. ``Skills`` is the only consumer of the read-only workspace;
    this helper does not attach filesystem or command capabilities to the agent.

    A caller selecting a model without a real context capacity must explicitly
    disable compaction. No generic fallback window is treated as model truth.
    """

    snapshots = _freeze_skill_snapshots(skill_snapshots)
    compaction = _compaction(model, target_fraction) if enable_compaction else None
    with ExitStack() as stack:
        capabilities: list[AbstractCapability[Any]] = []
        if snapshots:
            temporary = stack.enter_context(
                TemporaryDirectory(prefix="frisket-project-qa-skills-")
            )
            capabilities.append(_skill_capability(Path(temporary), snapshots))
        if compaction is not None:
            capabilities.append(compaction)
        yield capabilities
