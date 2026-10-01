"""Closed wire contracts for the text-only instruction skill library."""

from __future__ import annotations

from pydantic import Field

from frisket.contracts.http.models import WireModel


SKILL_LIBRARY_SCHEMA_VERSION = "frisket.skills.v1"


class SkillSaveRequest(WireModel):
    content: str = Field(min_length=1, max_length=131_072)
    enabled: bool = True


class SkillUpdateRequest(WireModel):
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    content: str = Field(min_length=1, max_length=131_072)


class SkillEnabledRequest(WireModel):
    expected_revision: int = Field(alias="expectedRevision", ge=1)
    enabled: bool


class SkillDeleteRequest(WireModel):
    expected_revision: int = Field(alias="expectedRevision", ge=1)


class InstructionSkill(WireModel):
    id: str
    name: str
    description: str
    content: str
    enabled: bool
    revision: int
    created_at: str = Field(alias="createdAt")
    updated_at: str = Field(alias="updatedAt")


class InstructionSkillList(WireModel):
    schema_version: str = Field(alias="schemaVersion")
    skills: list[InstructionSkill]
