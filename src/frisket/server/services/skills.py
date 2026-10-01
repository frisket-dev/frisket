"""Workspace-owned persistence for admitted, instruction-only SKILL.md files."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from frisket.contracts.http.skills import SKILL_LIBRARY_SCHEMA_VERSION


MAX_SKILL_BYTES = 128 * 1024
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ALLOWED_FRONTMATTER = {"name", "description"}


class SkillLibraryError(ValueError):
    status_code = 422


class SkillNotFound(SkillLibraryError):
    status_code = 404


class SkillRevisionConflict(SkillLibraryError):
    status_code = 409


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _frontmatter(content: str) -> tuple[str, str]:
    if "\x00" in content:
        raise SkillLibraryError("SKILL.md must be text without NUL bytes.")
    if not content.startswith("---\n"):
        raise SkillLibraryError("SKILL.md must start with name and description frontmatter.")
    end = content.find("\n---\n", 4)
    if end < 0:
        raise SkillLibraryError("SKILL.md frontmatter must end with a closing --- line.")
    try:
        metadata = yaml.safe_load(content[4:end])
    except yaml.YAMLError as exc:
        raise SkillLibraryError("SKILL.md frontmatter is not valid YAML.") from exc
    if not isinstance(metadata, dict):
        raise SkillLibraryError("SKILL.md frontmatter must be a mapping.")
    unsupported = sorted(set(metadata) - _ALLOWED_FRONTMATTER)
    if unsupported:
        raise SkillLibraryError(
            f"SKILL.md frontmatter has unsupported fields: {', '.join(unsupported)}."
        )
    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise SkillLibraryError(
            "SKILL.md name must use lowercase letters, digits, and hyphens."
        )
    if not isinstance(description, str) or not description.strip() or len(description) > 200:
        raise SkillLibraryError("SKILL.md description must be 1 to 200 characters.")
    if not content[end + 5 :].strip():
        raise SkillLibraryError("SKILL.md needs a Markdown instruction body.")
    return name, description.strip()


class SkillLibrary:
    """One controlled JSON document per workspace, never a client-chosen path."""

    def __init__(self, workspace_root: str | Path):
        self._path = Path(workspace_root) / ".frisket" / "skills.json"
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"schema_version": SKILL_LIBRARY_SCHEMA_VERSION, "skills": []}
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillLibraryError("Stored skills cannot be read safely.") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != SKILL_LIBRARY_SCHEMA_VERSION
            or not isinstance(document.get("skills"), list)
        ):
            raise SkillLibraryError("Stored skills have an unsupported format.")
        required = {
            "id", "name", "description", "content", "enabled", "revision",
            "created_at", "updated_at",
        }
        if any(not isinstance(record, dict) or not required <= set(record) for record in document["skills"]):
            raise SkillLibraryError("Stored skills have an unsupported format.")
        return document

    def _write(self, document: dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        fd, temporary = tempfile.mkstemp(prefix="skills-", suffix=".json", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": record["id"],
            "name": record["name"],
            "description": record["description"],
            "content": record["content"],
            "enabled": bool(record["enabled"]),
            "revision": int(record["revision"]),
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
        }

    def list(self) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            return {
                "schema_version": SKILL_LIBRARY_SCHEMA_VERSION,
                "skills": [self._public(record) for record in document["skills"]],
            }

    def create(self, content: str, *, enabled: bool = True) -> dict[str, Any]:
        if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
            raise SkillLibraryError("SKILL.md must be at most 128 KiB.")
        name, description = _frontmatter(content)
        with self._lock:
            document = self._read()
            if any(record.get("name") == name for record in document["skills"]):
                raise SkillLibraryError(f"A skill named '{name}' already exists.")
            now = _now()
            record = {
                "id": str(uuid.uuid4()),
                "name": name,
                "description": description,
                "content": content,
                "enabled": enabled,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            document["skills"].append(record)
            self._write(document)
            return self._public(record)

    def _record(self, document: dict[str, Any], skill_id: str) -> dict[str, Any]:
        record = next((item for item in document["skills"] if item.get("id") == skill_id), None)
        if not isinstance(record, dict):
            raise SkillNotFound("Skill not found.")
        return record

    @staticmethod
    def _check_revision(record: dict[str, Any], expected_revision: int) -> None:
        if int(record["revision"]) != expected_revision:
            raise SkillRevisionConflict("This skill changed. Refresh before saving.")

    def update(self, skill_id: str, *, expected_revision: int, content: str) -> dict[str, Any]:
        if len(content.encode("utf-8")) > MAX_SKILL_BYTES:
            raise SkillLibraryError("SKILL.md must be at most 128 KiB.")
        name, description = _frontmatter(content)
        with self._lock:
            document = self._read()
            record = self._record(document, skill_id)
            self._check_revision(record, expected_revision)
            if any(item.get("name") == name and item.get("id") != skill_id for item in document["skills"]):
                raise SkillLibraryError(f"A skill named '{name}' already exists.")
            record.update(
                name=name,
                description=description,
                content=content,
                revision=int(record["revision"]) + 1,
                updated_at=_now(),
            )
            self._write(document)
            return self._public(record)

    def set_enabled(self, skill_id: str, *, expected_revision: int, enabled: bool) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            record = self._record(document, skill_id)
            self._check_revision(record, expected_revision)
            record.update(enabled=enabled, revision=int(record["revision"]) + 1, updated_at=_now())
            self._write(document)
            return self._public(record)

    def delete(self, skill_id: str, *, expected_revision: int) -> None:
        with self._lock:
            document = self._read()
            record = self._record(document, skill_id)
            self._check_revision(record, expected_revision)
            document["skills"].remove(record)
            self._write(document)
