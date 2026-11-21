"""Staged blob-output helpers for action finalization boundaries."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from frisket.engine.store.blob_backend import BlobIntegrityError
from frisket.engine.store.project_blobs import publish_prepared_blob
from frisket.engine.store.transactions import require_transaction


@dataclass(frozen=True)
class RowBlobPlan:
    role: str
    content_digest: str
    staged_path: str | Path
    filename: str
    mime: str
    source_url: str | None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RowBlobOutput:
    primary: RowBlobPlan
    supplemental: tuple[RowBlobPlan, ...] = ()
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BlobWritePlan:
    digest: str
    temp_path: str | Path | None
    final_path: str | Path
    filename: str | None
    mime: str | None
    source_url: str | None
    metadata: Mapping[str, Any] = field(default_factory=dict)


def blob_digest(value: str) -> str:
    return value.removeprefix("sha256:")


def stage_blob_bytes(
    project: Any,
    data: bytes,
    *,
    role: str,
    filename: str,
    mime: str,
    source_url: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> RowBlobPlan:
    digest = hashlib.sha256(data).hexdigest()
    stage_dir = Path(project.path) / "tmp" / "blob-staging"
    stage_dir.mkdir(parents=True, exist_ok=True)
    staged_path = stage_dir / f"{digest}-{uuid.uuid4().hex}"
    staged_path.write_bytes(data)
    return RowBlobPlan(
        role=role,
        content_digest=digest,
        staged_path=staged_path,
        filename=filename,
        mime=mime,
        source_url=source_url,
        metadata=dict(metadata or {}),
    )


def commit_row_blob_plan(
    project: Any,
    plan: RowBlobPlan,
) -> str:
    """Persist staged blob bytes and record metadata in the caller's transaction."""
    require_transaction(project.db, "row blob plan commit")
    staged_path = Path(plan.staged_path)
    data = staged_path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    expected = blob_digest(plan.content_digest)
    if digest != expected:
        raise ValueError(f"staged blob digest mismatch for {plan.filename}")
    stored = project.blob_store.put(data)
    if stored != digest:
        raise BlobIntegrityError(
            "blob store returned a digest that does not match the submitted bytes"
        )
    publish_prepared_blob(
        project,
        digest=digest,
        size=len(data),
        filename=plan.filename,
        mime=plan.mime,
        source_url=plan.source_url,
        metadata=dict(plan.metadata),
    )
    return digest
