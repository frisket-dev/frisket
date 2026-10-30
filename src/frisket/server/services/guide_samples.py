"""First-party guide datasets and their isolated, idempotent bootstrap."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from contextlib import ExitStack
from dataclasses import dataclass

from filelock import FileLock

from frisket.sample_content.audio import SAMPLE_COUNCIL_AUDIO, council_audio_bytes
from frisket.sample_content.lawsuits import SAMPLE_LAWSUITS, lawsuit_pdf_bytes
from frisket.server.route_errors import RouteError
from frisket.server.services.import_csv import ImportCsvUploadService
from frisket.server.services.import_files import ImportFilesUploadService
from frisket.server.services.pdf_imports import PdfImportService
from frisket.server.services.sample_project import (
    _owned_upload,
    sample_council_meetings_csv,
    sample_docket_csv,
)


@dataclass(frozen=True)
class SampleFile:
    name: str
    mime: str
    data: bytes


SAMPLE_CATALOG = {
    "court-filings": {
        "id": "court-filings",
        "revision": 1,
        "title": "Court filings",
        "description": "Six fictional court filings ready to review and import.",
        "file_count": 7,
    },
    "council-recordings": {
        "id": "council-recordings",
        "revision": 1,
        "title": "Council recordings",
        "description": "Two council audio excerpts with source information.",
        "file_count": 3,
    },
}
SAMPLE_IDS = tuple(SAMPLE_CATALOG)


def metadata(sample_id: str, revision: int = 1) -> dict:
    info = SAMPLE_CATALOG.get(sample_id)
    if info is None or revision != info["revision"]:
        raise RouteError(404, "Unknown guide sample or revision")
    return info.copy()


def files(sample_id: str, revision: int = 1) -> list[SampleFile]:
    metadata(sample_id, revision)
    if sample_id == "court-filings":
        return [
            SampleFile(x.filename, "application/pdf", lawsuit_pdf_bytes(x))
            for x in SAMPLE_LAWSUITS
        ] + [SampleFile("expected.csv", "text/csv", sample_docket_csv().encode())]
    if sample_id != "council-recordings":
        raise RuntimeError(f"No files defined for guide sample {sample_id}")
    return [
        SampleFile(x.filename, "audio/mpeg", council_audio_bytes(x.filename))
        for x in SAMPLE_COUNCIL_AUDIO
    ] + [
        SampleFile(
            "council-meetings.csv", "text/csv", sample_council_meetings_csv().encode()
        )
    ]


def sample_metadata(sample_id: str, revision: int = 1) -> dict:
    info = metadata(sample_id, revision).copy()
    assets = files(sample_id, revision)
    provenance = {
        item.filename: {
            "source_url": item.source_url,
            "license": item.license,
            "author": item.author,
        }
        for item in SAMPLE_COUNCIL_AUDIO
    }
    info["files"] = [
        {
            "filename": item.name,
            "media_type": item.mime,
            "size": len(item.data),
            **provenance.get(item.name, {}),
        }
        for item in assets
    ]
    info["total_bytes"] = sum(len(item.data) for item in assets)
    return info


def download(sample_id: str, revision: int = 1) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in files(sample_id, revision):
            entry = zipfile.ZipInfo(item.name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, item.data)
    return output.getvalue()


class GuideSampleService:
    def __init__(self, workspace, pdf: PdfImportService):
        self.workspace = workspace
        self.pdf = pdf
        self.csv = ImportCsvUploadService(workspace)
        self.file_import = ImportFilesUploadService(workspace)

    def prepare(
        self,
        project_id: str,
        sample_id: str,
        revision: int,
        operation_id: str,
        actor: str,
    ) -> dict:
        info = metadata(sample_id, revision)
        storage_identity = self.workspace.get(project_id).storage_identity
        if not operation_id or len(operation_id) > 128 or not operation_id.strip():
            raise RouteError(
                422, "A nonempty operation_id of at most 128 characters is required"
            )
        digest = hashlib.sha256(
            json.dumps(
                [actor, project_id, operation_id], separators=(",", ":")
            ).encode()
        ).hexdigest()
        root = self.workspace.root / ".guide-sample-operations"
        root.mkdir(parents=True, exist_ok=True)
        path = root / (digest + ".json")
        with FileLock(str(root / (digest + ".lock")), timeout=-1):
            if path.exists():
                record = json.loads(path.read_text())
                if record.get("storage_identity") != storage_identity:
                    raise RouteError(
                        409, "The sample operation refers to a different project"
                    )
                if record["sample_id"] != sample_id or record["revision"] != revision:
                    raise RouteError(
                        409, "Operation was already used for a different sample"
                    )
            else:
                record = {
                    "sample_id": sample_id,
                    "revision": revision,
                    "storage_identity": storage_identity,
                    "state": "project",
                }
                self._save(path, record)
            if record["state"] == "complete":
                self.workspace.get(project_id)
                result = record["result"]
                destination = result["destination"]
                if destination["kind"] == "pdf_import_draft":
                    status = self.pdf.status(
                        result["project_id"], destination["draft_id"]
                    )
                    if status["state"] != "open":
                        raise RouteError(
                            409,
                            "The sample import is no longer open. Start a new sample to try again.",
                        )
                    destination = {
                        **destination,
                        "state": "ready" if status["can_review_text"] else "pending",
                    }
                    result = {**result, "destination": destination}
                return result
            self.workspace.get(project_id)
            pid = project_id
            items = files(sample_id, revision)
            if record["state"] == "pdf":
                destination = self._stage_pdf(pid, record["draft_id"], items)
                result = {"project_id": pid, "destination": destination}
                record.update(state="complete", result=result)
                self._save(path, record)
                return result
            with ExitStack() as stack:
                uploads = [
                    _owned_upload(stack, filename=x.name, mime=x.mime, data=x.data)
                    for x in items
                ]
                if sample_id == "court-filings":
                    response = self.csv.upload_csv(
                        pid, upload=uploads[-1], sheet_name="Expected"
                    )
                    if (
                        response.status_code != 200
                        or "sheet_id" not in response.payload
                    ):
                        raise RuntimeError("Could not import sample expected data")
                    draft = self.pdf.create(
                        pid,
                        expected_files=len(items) - 1,
                        expected_bytes=sum(len(x.data) for x in items[:-1]),
                        source_label=info["title"],
                        creation_key=json.dumps(
                            [
                                actor,
                                storage_identity,
                                sample_id,
                                revision,
                                operation_id,
                            ],
                            separators=(",", ":"),
                        ),
                    )
                    draft_id = draft["draft_id"]
                    record.update(state="pdf", draft_id=draft_id)
                    self._save(path, record)
                else:
                    response = self.csv.upload_csv(
                        pid, upload=uploads[-1], sheet_name="Council meetings"
                    )
                    if (
                        response.status_code != 200
                        or "sheet_id" not in response.payload
                    ):
                        raise RuntimeError("Could not import sample meeting data")
                    response = self.file_import.upload_files(
                        pid, files=uploads[:-1], sheet_name="Council audio"
                    )
                    if (
                        response.status_code != 200
                        or "sheet_id" not in response.payload
                    ):
                        raise RuntimeError("Could not import sample recordings")
                    destination = {
                        "kind": "sheet",
                        "sheet_id": response.payload["sheet_id"],
                        "guide_id": "council-recordings-to-transcripts",
                        "step_id": "transcribe",
                    }
            if sample_id == "court-filings":
                destination = self._stage_pdf(pid, record["draft_id"], items)
            result = {"project_id": pid, "destination": destination}
            record.update(state="complete", result=result)
            self._save(path, record)
            return result

    def _stage_pdf(self, pid, draft_id, items):
        selected = items[:-1]
        ids = [f"file-{i}" for i in range(len(selected))]
        self.pdf.append_manifest(
            pid,
            draft_id,
            batch_id="guide-manifest",
            files=[
                {
                    "client_file_id": n,
                    "logical_path": f.name,
                    "filename": f.name,
                    "size": len(f.data),
                }
                for n, f in zip(ids, selected, strict=True)
            ],
        )
        self.pdf.seal_manifest(pid, draft_id)
        with ExitStack() as stack:
            uploads = [
                _owned_upload(stack, filename=f.name, mime=f.mime, data=f.data)
                for f in selected
            ]
            self.pdf.upload(
                pid,
                draft_id,
                uploads,
                file_ids=ids,
                logical_paths=[f.name for f in selected],
                batch_id="guide-upload",
            )
        status = self.pdf.analyze_staged(pid, draft_id)
        return {
            "kind": "pdf_import_draft",
            "draft_id": draft_id,
            "stage": "check_text",
            "state": "ready" if status["can_review_text"] else "pending",
            "guide_id": "court-filings-to-table",
            "step_id": "check-text",
        }

    @staticmethod
    def _save(path, record):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, sort_keys=True))
        tmp.replace(path)
