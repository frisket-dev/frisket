"""Admit source-local PDF plans to the ordinary durable import queue."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, ContextManager, Mapping, Sequence

from filelock import FileLock

from frisket.actions.system import typed_action_for_request
from frisket.contracts.action import ActionResult
from frisket.engine.executor.action_jobs import (
    ActionJobEnvelope,
    mark_action_job_enqueued,
    reserve_typed_action_job,
)
from frisket.engine.jobs.queue import PDF_SPLIT_APPLY_KIND
from frisket.engine.store.disk_capacity import require_disk_headroom
from frisket.engine.store.blob_backend import validate_blob_digest
from frisket.engine.store.document_text import document_text_role
from frisket.engine.store.import_intake import (
    ImportIntakeHeader,
    import_intake_dir,
    read_import_header,
    write_import_header,
)
from frisket.engine.store.pdf_split import PdfSplitSource, PdfSplitWriter
from frisket.engine.store.import_sessions import ImportSessionStore
from frisket.engine.store.pdf_split_intake import (
    pdf_split_status,
    read_pdf_split_plan,
    split_plan_hash,
    write_pdf_split_plan,
)
from frisket.engine.store.receipts import ReceiptStore
from frisket.engine.store.value_codec import encode_stored_value
from frisket.pdf_packets.names import child_filename as _child_filename
from frisket.pdf_packets.source_map import SourceMap, SourceRange
from frisket.server.services.pdf_packet_sources import AdmittedPacketSource
from frisket.server.services.pdf_imports.storage import PdfImportDraftError
from frisket.server.workspace import Workspace


@dataclass(frozen=True)
class PdfSplitPassthrough:
    """Host-admitted mixed-file occurrence, published by ordinary blob staging."""

    key: str
    sha256: str | None
    filename: str
    size: int
    mime: str
    open_source: Callable[[], ContextManager[Path]] | None
    source_url: str | None = None
    error: str | None = None
    provider: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    page_count: int | None = None


def _unique_name(name: str, used: set[str]) -> str:
    candidate = name
    suffix = 2
    while candidate in used:
        candidate = f"{name}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _column_plan(project, destination, sources, carried_column_ids, organization):
    sheet_id = destination.sheet_id
    existing = (
        [] if sheet_id is None else list(project.columns(sheet_id, include_hidden=True))
    )
    used = {str(column["name"]) for column in existing}
    columns = []
    roles = {}
    file_ids = {
        source.source_column_id
        for source in sources
        if source.source_sheet_id == sheet_id
    }
    media_column = (
        next((column for column in existing if column["id"] in file_ids), None)
        if len(file_ids) == 1
        else None
    )
    designated_text = (
        None
        if media_column is None
        else document_text_role(project, source_column_id=int(media_column["id"]))
    )
    role_specs = [
        ("filename", "text"),
        ("media", "file"),
        (
            "page" if organization == "pages" else "pages",
            "integer" if organization == "pages" else "text",
        ),
        ("source", "link"),
        ("text", "text"),
    ]
    generated_ids = set()
    for role, type_name in role_specs:
        candidate = (
            media_column
            if role == "media"
            else next(
                (
                    column
                    for column in existing
                    if designated_text is not None
                    and role == "text"
                    and column["id"] == designated_text.text_column_id
                ),
                None,
            )
        )
        if candidate is None:
            candidate = next(
                (
                    column
                    for column in existing
                    if column["semantic_type"] == f"pdf_split.{role}"
                    and column["type"] == type_name
                ),
                None,
            )
        if candidate is not None:
            name = str(candidate["name"])
            generated_ids.add(int(candidate["id"]))
        else:
            name = _unique_name(role, used)
        roles[role] = name
        columns.append(
            {"name": name, "type": type_name, "semantic_type": f"pdf_split.{role}"}
        )
    carried = {}
    source_sheets = {source.source_sheet_id for source in sources}
    if carried_column_ids and (None in source_sheets or len(source_sheets) != 1):
        raise ValueError("carried columns require one admitted source sheet")
    for column_id in carried_column_ids:
        column = project.get_column(int(column_id))
        if (
            column is None
            or not column["active"]
            or column["hidden"]
            or column["sheet_id"] not in source_sheets
        ):
            raise ValueError(
                "carried column is not available in the admitted source sheet"
            )
        if column["sheet_id"] == sheet_id and column_id not in generated_ids:
            name = str(column["name"])
        else:
            name = _unique_name(str(column["name"]), used)
        carried[str(column_id)] = name
        columns.append(
            {"name": name, "type": str(column["type"]), "format": column["format"]}
        )
    return columns, roles, carried


class PdfPacketCommitter:
    def __init__(self, workspace: Workspace) -> None:
        self._workspace = workspace

    @staticmethod
    def _identity(project, idempotency_key: str) -> tuple[str, Path]:
        ref = (
            "import-"
            + hashlib.sha256(
                f"{project.storage_identity}:{idempotency_key}".encode()
            ).hexdigest()[:32]
        )
        return ref, import_intake_dir(project.path, ref)

    def rollback_before_plan(
        self,
        project_id: str,
        *,
        idempotency_key: str,
        restore_draft: Callable[[], None],
    ) -> None:
        """Reopen a rejected draft; preserve frozen plans for normal replay."""
        ref, directory = self._identity(
            self._workspace.get(project_id), idempotency_key
        )
        directory.parent.mkdir(exist_ok=True)
        with FileLock(str(directory.parent / f".{ref}.lock")):
            if (directory / "pdf-split.json").exists():
                return
            restore_draft()
            if directory.exists():
                shutil.rmtree(directory)

    def resume(
        self,
        project_id: str,
        *,
        idempotency_key: str,
        pdf_preparation: Mapping[str, Any] | None = None,
        request_context: Any = None,
    ) -> dict[str, Any] | None:
        """Recover an admitted plan before consulting an expired draft inventory."""
        project = self._workspace.get(project_id)
        ref, directory = self._identity(project, idempotency_key)
        if not (directory / "pdf-split.json").is_file():
            return None
        with FileLock(str(directory.parent / f".{ref}.lock")):
            plan = read_pdf_split_plan(directory)
            if (
                plan["project_id"] != project_id
                or plan["idempotency_key"] != f"pdf-split:{idempotency_key}"
            ):
                raise ValueError("split plan belongs to another admitted request")
            return self._bootstrap(
                project,
                project_id,
                ref,
                directory,
                plan,
                split_plan_hash(plan),
                pdf_preparation,
                request_context,
            )

    def start(
        self,
        project_id: str,
        *,
        sources: Sequence[AdmittedPacketSource],
        source_map: SourceMap | None,
        ranges: Sequence[SourceRange],
        destination: Any,
        idempotency_key: str,
        request_context: Any = None,
        carried_column_ids: Sequence[int] = (),
        name_pattern: str = "{packet}-{index}.pdf",
        organization: str = "pages",
        cached_pages: Mapping[str, Mapping[int, Any]] | None = None,
        passthrough: Sequence[PdfSplitPassthrough] = (),
        pdf_preparation: Mapping[str, Any] | None = None,
        native_pages: Mapping[str, Mapping[int, Path]] | None = None,
        child_names: Mapping[str, Sequence[str]] | None = None,
        source_order: Sequence[str] | None = None,
        skipped_duplicates: int = 0,
        preserve_originals: bool = False,
    ) -> dict[str, Any]:
        project = self._workspace.get(project_id)
        if type(skipped_duplicates) is not int or skipped_duplicates < 0:
            raise ValueError("skipped duplicate count must be nonnegative")
        by_key = {source.key: source for source in (*sources, *passthrough)}
        if len(by_key) != len(sources) + len(passthrough):
            raise ValueError("source occurrence keys must be distinct")
        ordered_keys = list(by_key) if source_order is None else list(source_order)
        if len(ordered_keys) != len(by_key) or set(ordered_keys) != set(by_key):
            raise ValueError(
                "source order must name every admitted occurrence exactly once"
            )
        if (not sources and (ranges or not passthrough)) or (
            sources
            and (
                source_map is None
                or tuple(source.key for source in sources)
                != tuple(source.key for source in source_map.occurrences)
            )
        ):
            raise ValueError("split sources differ from the admitted source map")
        grouped = {source.key: [] for source in sources}
        for item in ranges:
            admitted = next(
                (source for source in sources if source.key == item.occurrence.key),
                None,
            )
            if (
                admitted is None
                or item.occurrence not in source_map.occurrences
                or not admitted.page_start
                <= item.start
                <= item.end
                <= admitted.page_end
            ):
                raise ValueError("split range is outside its admitted source")
            grouped[admitted.key].append((item.start, item.end))
        for source in sources:
            expected = source.page_start
            for start, end in grouped[source.key]:
                if start != expected:
                    raise ValueError(
                        "split ranges must cover each admitted source exactly once"
                    )
                expected = end + 1
            if expected != source.page_end + 1:
                raise ValueError("split ranges do not cover an admitted source")
            if preserve_originals and (
                source.page_start != 1
                or source.page_end != source.page_count
                or len(grouped[source.key]) != 1
            ):
                raise ValueError(
                    "keeping originals requires one complete range per source"
                )
        if len(set(carried_column_ids)) != len(carried_column_ids):
            raise ValueError("carried column IDs must be distinct")
        request = {
            "sources": [
                {
                    "key": source.key,
                    "sha256": source.sha256,
                    "value": source.frozen_value,
                    "start": source.page_start,
                    "end": source.page_end,
                    "mime": source.mime,
                    "provider": source.provider,
                    "metadata": dict(source.metadata),
                }
                for source in sources
            ],
            "ranges": [(item.occurrence.key, item.start, item.end) for item in ranges],
            "destination": destination.model_dump(mode="json"),
            "carried_column_ids": list(carried_column_ids),
            "name_pattern": name_pattern,
            "organization": organization,
            "source_order": ordered_keys,
            "skipped_duplicates": skipped_duplicates,
            "preserve_originals": preserve_originals,
            "child_names": {}
            if child_names is None
            else {key: list(value) for key, value in child_names.items()},
            "passthrough": [
                (
                    source.key,
                    source.sha256,
                    source.filename,
                    source.size,
                    source.mime,
                    source.error,
                    source.source_url,
                    source.provider,
                    dict(source.metadata),
                    source.page_count,
                )
                for source in passthrough
            ],
        }
        fingerprint = split_plan_hash(request)
        ref, directory = self._identity(project, idempotency_key)
        directory.parent.mkdir(exist_ok=True)
        with FileLock(str(directory.parent / f".{ref}.lock")):
            if (directory / "pdf-split.json").is_file():
                old = read_pdf_split_plan(directory)
                if old["request_fingerprint"] != fingerprint:
                    raise ValueError(
                        "idempotency key was already used for another split plan"
                    )
                return self._bootstrap(
                    project,
                    project_id,
                    ref,
                    directory,
                    old,
                    split_plan_hash(old),
                    pdf_preparation,
                    request_context,
                )
            if destination.kind == "new_sheet":
                sheet_name = destination.name
            else:
                sheet = project.db.execute(
                    "SELECT name FROM sheets WHERE id=? AND hidden=0",
                    (destination.sheet_id,),
                ).fetchone()
                if sheet is None:
                    raise PdfImportDraftError(
                        409,
                        "The destination sheet is unavailable. Choose another sheet.",
                    )
                sheet_name = str(sheet[0])
            columns, roles, carried = _column_plan(
                project, destination, sources, carried_column_ids, organization
            )
            if any(source.error for source in passthrough):
                used_names = {str(column["name"]) for column in columns}
                if destination.sheet_id is not None:
                    used_names.update(
                        str(column["name"])
                        for column in project.columns(
                            destination.sheet_id, include_hidden=True
                        )
                    )
                roles["error"] = _unique_name("error", used_names)
                columns.append({"name": roles["error"], "type": "text"})
            directory.mkdir(exist_ok=True)
            (directory / "staged").mkdir(exist_ok=True)
            (directory / "snapshots").mkdir(exist_ok=True)
            planned = []
            for ordinal, source_key in enumerate(ordered_keys):
                source = by_key[source_key]
                is_pdf = isinstance(source, AdmittedPacketSource)
                is_image = (
                    not is_pdf
                    and source.sha256 is not None
                    and source.mime.startswith("image/")
                    and source.error is None
                )
                if is_image and source.page_count not in (None, 1):
                    raise ValueError("image source must designate one page")
                if source.sha256 is not None:
                    validate_blob_digest(source.sha256)
                canonical = project.db.execute(
                    "SELECT size FROM blobs WHERE hash=?", (source.sha256,)
                ).fetchone()
                if source.sha256 is None:
                    if (
                        is_pdf
                        or not source.error
                        or source.size != 0
                        or source.open_source is not None
                    ):
                        raise ValueError("an acquisition error must have no file bytes")
                elif canonical is None:
                    staged = directory / "staged" / source.sha256
                    digest = hashlib.sha256()
                    size = 0
                    with (
                        source.open_source() as path,
                        Path(path).open("rb") as reader,
                        staged.open("wb") as sink,
                    ):
                        while chunk := reader.read(1024 * 1024):
                            require_disk_headroom(directory, len(chunk))
                            sink.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    if digest.hexdigest() != source.sha256 or size != source.size:
                        staged.unlink(missing_ok=True)
                        raise ValueError("admitted source bytes changed")
                elif int(canonical[0]) != source.size:
                    raise ValueError("admitted source size changed")
                context = {}
                if is_pdf and source.source_row_id is not None:
                    for column_id, output_name in carried.items():
                        values = project.get_values(
                            source.source_sheet_id,
                            int(column_id),
                            [source.source_row_id],
                        )
                        kind, value = encode_stored_value(
                            values.get(source.source_row_id)
                        )
                        if isinstance(value, str) and len(value) > 4096:
                            relative = f"snapshots/{ordinal}-{column_id}.txt"
                            (directory / relative).write_text(value)
                            context[output_name] = {
                                "kind": kind,
                                "file": relative,
                                "sha256": hashlib.sha256(value.encode()).hexdigest(),
                            }
                        else:
                            context[output_name] = {"kind": kind, "value": value}
                children = []
                if is_pdf:
                    for index, (start, end) in enumerate(grouped[source.key], 1):
                        name = (
                            str(child_names[source.key][index - 1])
                            if child_names is not None and source.key in child_names
                            else _child_filename(
                                name_pattern, source.filename, index, start, end
                            )
                        )
                        if preserve_originals or organization == "pages":
                            name = source.filename
                        if (
                            not name
                            or len(name) > 512
                            or (not preserve_originals and Path(name).name != name)
                        ):
                            raise ValueError("invalid split child filename")
                        children.append(
                            {
                                "start": start,
                                "end": end,
                                "filename": name,
                                "keep_original": preserve_originals,
                            }
                        )
                elif is_image:
                    children.append(
                        {
                            "start": 1,
                            "end": 1,
                            "filename": source.filename,
                            "keep_original": True,
                        }
                    )
                else:
                    children.append({"filename": source.filename})
                cache = {}
                for page, content in (cached_pages or {}).get(source.key, {}).items():
                    relative = f"snapshots/{ordinal}-page-{int(page)}.json"
                    payload = (
                        {"text": content.text, "positions": content.positions}
                        if hasattr(content, "text")
                        else dict(content)
                    )
                    (directory / relative).write_text(
                        json.dumps(payload, allow_nan=False)
                    )
                    cache[str(int(page))] = relative
                for page, source_cache in (
                    (native_pages or {}).get(source.key, {}).items()
                ):
                    relative = f"snapshots/{ordinal}-page-{int(page)}.json"
                    with (
                        Path(source_cache).open("rb") as reader,
                        (directory / relative).open("wb") as sink,
                    ):
                        while chunk := reader.read(1024 * 1024):
                            require_disk_headroom(directory, len(chunk))
                            sink.write(chunk)
                    cache[str(int(page))] = relative
                planned.append(
                    {
                        "key": source.key,
                        "kind": "pdf"
                        if is_pdf
                        else "image"
                        if is_image
                        else "error"
                        if source.sha256 is None
                        else "file",
                        "sha256": source.sha256,
                        "filename": source.filename,
                        "size": source.size,
                        "mime": source.mime,
                        "source_url": source.source_url,
                        "source_sheet_id": source.source_sheet_id if is_pdf else None,
                        "source_row_id": source.source_row_id if is_pdf else None,
                        "source_column_id": source.source_column_id if is_pdf else None,
                        "frozen_value": source.frozen_value if is_pdf else None,
                        "page_count": source.page_count
                        if is_pdf
                        else 1
                        if is_image
                        else None,
                        "prepared_ref_id": source.prepared_ref_id if is_pdf else None,
                        "prepared_pages": list(source.prepared_pages) if is_pdf else [],
                        "children": children,
                        "context": context,
                        "cache": cache,
                        "error": None if is_pdf else source.error,
                        "provider": source.provider,
                        "metadata": dict(source.metadata),
                    }
                )
                if is_pdf and source.prepared_ref_id is not None:
                    prepared = project.db.execute(
                        "SELECT artifact.metadata FROM prepared_content_refs ref "
                        "JOIN prepared_content_sets content ON content.id=ref.set_id "
                        "JOIN source_artifacts artifact ON artifact.id=content.source_artifact_id WHERE ref.id=?",
                        (source.prepared_ref_id,),
                    ).fetchone()
                    metadata = (
                        {} if prepared is None else json.loads(prepared[0] or "{}")
                    )
                    planned[-1]["prepared_engine"] = metadata.get("engine")
                    planned[-1]["prepared_token_granularity"] = metadata.get(
                        "ocr_token_granularity"
                    )
            plan = {
                "request_fingerprint": fingerprint,
                "idempotency_key": f"pdf-split:{idempotency_key}",
                "carried_column_ids": list(carried_column_ids),
                "project_id": project_id,
                "sheet_name": sheet_name,
                "destination": request["destination"],
                "columns": columns,
                "roles": roles,
                "organization": organization,
                "sources": planned,
                "skipped_duplicates": skipped_duplicates,
                "preparation_required": bool(pdf_preparation),
            }
            plan_hash = write_pdf_split_plan(directory, plan)
            return self._bootstrap(
                project,
                project_id,
                ref,
                directory,
                plan,
                plan_hash,
                pdf_preparation,
                request_context,
            )

    def _bootstrap(
        self,
        project,
        project_id,
        ref,
        directory,
        plan,
        plan_hash,
        pdf_preparation,
        request_context,
    ):
        if (directory / "manifest.json").is_file():
            header = read_import_header(directory)
            if (
                header.storage_identity != project.storage_identity
                or header.project_id != project_id
            ):
                raise ValueError("split intake belongs to another project")
            envelope = ActionJobEnvelope.from_json(header.envelope)
            if envelope.action.get("params", {}).get("plan_hash") != plan_hash:
                raise ValueError("split receipt differs from its frozen plan")
        else:
            if plan.get("preparation_required") and pdf_preparation is None:
                raise ValueError(
                    "original approved PDF preparation facts are required to resume admission"
                )
            action = typed_action_for_request(
                {
                    "action_id": "import.packet_split",
                    "scope": {"kind": "project"},
                    "sheet_name": plan["sheet_name"],
                    "params": {
                        "source_ref": ref,
                        "plan_hash": plan_hash,
                        "destination": plan["destination"]["kind"],
                        "destination_sheet_id": plan["destination"].get("sheet_id"),
                        "carried_column_ids": plan["carried_column_ids"],
                        "organization": plan["organization"],
                    },
                    "idempotency_key": plan["idempotency_key"],
                }
            )
            execution_context = (
                self._workspace.edition_execution_composition_context_for(
                    request_context
                )
            )
            envelope = reserve_typed_action_job(
                project,
                project_id,
                action,
                edition_run_context=execution_context.edition_snapshot,
            )
            if isinstance(envelope, ActionResult):
                raise ValueError(
                    envelope.errors[0].message
                    if envelope.errors
                    else "split reservation is already terminal"
                )
            header = ImportIntakeHeader(
                project_id,
                project.storage_identity,
                envelope.to_json(),
                max_rows=sum(len(source["children"]) for source in plan["sources"]),
                destination_sheet_id=plan["destination"].get("sheet_id"),
                staged_files=True,
                pdf_preparation=None
                if pdf_preparation is None
                else dict(pdf_preparation),
            )
            write_import_header(directory, header)
        session = ImportSessionStore(project).get(ref)
        if session is not None and session.state in ("completed", "kept", "removed"):
            return self._result(project, ref)
        stored = ReceiptStore(project).find_by_id(envelope.receipt_id)
        if stored is None:
            raise ValueError("split receipt is unavailable")
        if stored.status == "queued":
            ReceiptStore(project).update_body_status(
                stored.parsed().model_copy(update={"status": "running"}),
                require_status="queued",
            )
        elif stored.status != "running":
            raise ValueError("split reservation is already terminal")
        if session is None:
            try:
                PdfSplitWriter.start(
                    project,
                    session_id=ref,
                    writer_authority=f"pdf-split-admission:{ref}",
                    columns=plan["columns"],
                    project_id=project_id,
                    idempotency_key=envelope.idempotency_key,
                    params_hash=envelope.params_hash,
                    action_id=envelope.action_id,
                    receipt_id=envelope.receipt_id,
                    sources=[
                        PdfSplitSource(
                            source["key"],
                            len(source["children"]),
                            source["source_sheet_id"],
                            source["source_row_id"],
                            source["source_column_id"],
                            source["frozen_value"],
                        )
                        for source in plan["sources"]
                    ],
                    sheet_id=plan["destination"].get("sheet_id"),
                    replace=plan["destination"]["kind"] == "replace",
                    sheet_name=plan["sheet_name"],
                )
            except BaseException:
                current = ReceiptStore(project).find_by_id(envelope.receipt_id)
                if current is not None and current.status == "running":
                    ReceiptStore(project).update_body_status(
                        current.parsed().model_copy(update={"status": "failed"}),
                        require_status="running",
                    )
                raise
        dedupe_key = f"pdf-split:{project_id}:{ref}:{plan_hash}"
        queued = self._workspace.queue.find_job_by_refs(
            PDF_SPLIT_APPLY_KIND,
            statuses=("queued", "running"),
            project_id=project_id,
            storage_org_id=getattr(self._workspace, "queue_storage_org_id", None),
            dedupe_key=dedupe_key,
        )
        extra = getattr(self._workspace, "queue_payload_extra", {})
        job_id = (
            queued.id
            if queued is not None
            else self._workspace.queue.enqueue(
                PDF_SPLIT_APPLY_KIND,
                {
                    **extra,
                    "project_id": project_id,
                    "workspace_root": str(self._workspace.root),
                    "import_ref": ref,
                    "plan_hash": plan_hash,
                    "receipt_id": envelope.receipt_id,
                    "action_kind": envelope.action_kind,
                    "dedupe_key": dedupe_key,
                },
            )
        )
        provider_state = {
            **dict(header.provider_state or {}),
            "kind": "pdf_split",
            "job_id": job_id,
        }
        provider_state.setdefault("preparation_pending", bool(header.pdf_preparation))
        write_import_header(directory, replace(header, provider_state=provider_state))
        mark_action_job_enqueued(
            project,
            receipt_id=envelope.receipt_id,
            job_id=job_id,
            job_kind=PDF_SPLIT_APPLY_KIND,
        )
        return self._result(project, ref)

    @staticmethod
    def _result(project, ref):
        header = read_import_header(import_intake_dir(project.path, ref))
        result = pdf_split_status(project, ref)
        result["job_id"] = (header.provider_state or {}).get("job_id")
        return result


__all__ = ["PdfPacketCommitter", "PdfSplitPassthrough"]
