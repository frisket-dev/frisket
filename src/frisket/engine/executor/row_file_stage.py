"""Schema-directed row file lowering over the existing invocation blob stager."""

from __future__ import annotations

import base64
import copy
import types
from dataclasses import replace
from typing import Annotated, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter

from frisket.actions.types import (
    Outcome,
    RowError,
    StagedFile,
    StagedImage,
    StagedAudio,
    StagedVideo,
)
from frisket.engine.executor.blob_outputs import RowBlobOutput
from frisket.engine.executor.import_blob_stage import AdmittedImportBlobStager
from frisket.engine.store.artifact_timeline import canonical_json_hash
from frisket.engine.store.blob_backend import sha256_blob_path
from frisket.engine.store.media_blobs import media_cell
from frisket.engine.store.row_files import (
    RowFileBlob,
    RowFileOccurrence,
    row_file_descriptors,
    register_returned_row_files,
)


class RowFileStager:
    def __init__(self, project, *, preview=False):
        self.project = project
        self.preview = preview
        self._stager = AdmittedImportBlobStager()
        self._files = {}
        self._pending = {}
        self._observations = {}
        self._preview_count = 0
        self._preview_bytes = 0
        self.preview_omitted = 0

    def observations(self, row_id):
        return copy.deepcopy(self._observations.get(row_id, []))

    def discard_row(self, row_id):
        self._pending.pop(row_id, None)

    def close(self):
        self._stager.close()

    def bind_row(self, row_id):
        if type(row_id) is not int or row_id <= 0:
            raise RowError(
                "invalid_input_ref", "File staging requires an admitted row."
            )
        self._stager._require_open()
        return _BoundRowFileStager(self, row_id)

    def _stage(self, row_id, output, image, media_type=None):
        if not isinstance(output, RowBlobOutput):
            raise TypeError("row file staging requires its host blob output")
        blobs = []
        handles = []
        for plan in (output.primary, *output.supplemental):
            with open(plan.staged_path, "rb") as stream:
                handle = self._stager.stage(
                    stream, filename=plan.filename, mime=plan.mime
                )
            blob = self._stager._admitted(handle)
            if blob.digest != plan.content_digest.removeprefix("sha256:"):
                raise RowError("invalid_output", "Staged file bytes changed.")
            metadata = {**blob.metadata, **copy.deepcopy(dict(plan.metadata))}
            blob = replace(blob, metadata=metadata, source_url=plan.source_url)
            self._stager._manifest[handle] = blob
            handles.append(handle)
            blobs.append(
                RowFileBlob(
                    blob_hash=blob.digest,
                    size=blob.size,
                    filename=blob.filename,
                    mime=blob.mime,
                    source_url=blob.source_url,
                    metadata=metadata,
                    role=plan.role,
                )
            )
        handle = handles[0]
        if image:
            if not blobs[0].mime.startswith("image/"):
                raise RowError("invalid_output", "Staged image requires image bytes.")
            from PIL import Image

            try:
                with Image.open(self._stager._admitted(handle).path) as observed:
                    actual_mime = Image.MIME.get(observed.format)
                    observed.verify()
                if actual_mime != blobs[0].mime:
                    raise ValueError("image MIME differs from observed bytes")
            except Exception:
                raise RowError(
                    "invalid_output", "Staged image bytes are invalid."
                ) from None
            image_handle = StagedImage(handle.size)
            self._stager._manifest[image_handle] = self._stager._manifest.pop(handle)
            handle = image_handle
            handles[0] = handle
        if media_type is not None:
            if (
                image
                or media_type not in {"audio", "video"}
                or not blobs[0].mime.startswith(media_type + "/")
            ):
                raise RowError(
                    "invalid_output",
                    "Staged media MIME does not match its declared kind.",
                )
            media_handle = (StagedAudio if media_type == "audio" else StagedVideo)(
                handle.size
            )
            self._stager._manifest[media_handle] = self._stager._manifest.pop(handle)
            handle = media_handle
            handles[0] = handle
        self._files[handle] = (
            row_id,
            tuple(handles),
            tuple(blobs),
            copy.deepcopy(dict(output.facts)),
        )
        return handle

    def _lower_file(self, row_id, key, path, handle):
        owned = self._files.get(handle)
        if owned is None or owned[0] != row_id:
            raise RowError(
                "invalid_output", "File output was not issued to this admitted row."
            )
        _, handles, blobs, facts = owned
        primary = blobs[0]
        if self.preview:
            if not isinstance(handle, StagedImage):
                raise RowError(
                    "invalid_output", "Only image files support local preview."
                )
            blob = self._stager._admitted(handle)
            if (
                primary.mime != "image/jpeg"
                or self._preview_count >= 12
                or self._preview_bytes + blob.size > 2 * 1024 * 1024
            ):
                self.preview_omitted += 1
                return {
                    "preview_omitted": True,
                    "mime": primary.mime,
                    "filename": primary.filename,
                }
            self._preview_count += 1
            self._preview_bytes += blob.size
            return {
                "inline_data_url": "data:image/jpeg;base64,"
                + base64.b64encode(blob.path.read_bytes()).decode("ascii"),
                "mime": primary.mime,
                "filename": primary.filename,
            }
        descriptor = RowFileOccurrence(
            row_id=row_id,
            output_key=key,
            item_path=list(path),
            primary=primary,
            supplemental=list(blobs[1:]),
            facts=facts,
        ).model_dump(mode="json")
        self._pending.setdefault(row_id, []).append((descriptor, handles))
        return media_cell(
            primary.blob_hash, mime=primary.mime, filename=primary.filename
        )

    def capture(self, results, *, row_id, fields, output_names):
        """Promote bytes, not SQL metadata, before the returned checkpoint."""
        pending = self._pending.pop(row_id, None)
        if pending is None:
            # This branch is reached only for the host's decoded returned response.
            verify_row_files(
                self.project,
                results,
                row_id=row_id,
                fields=fields,
                output_names=output_names,
            )
            return
        expected = [descriptor for descriptor, _handles in pending]
        observed = [
            item
            for payload in results.values()
            for item in payload.get("row_files", [])
        ]
        if observed != expected:
            raise RowError(
                "invalid_output", "File output association changed before publication."
            )
        for descriptor, handles in pending:
            for expected_blob, handle in zip(
                [descriptor["primary"], *descriptor["supplemental"]],
                handles,
                strict=True,
            ):
                blob = self._stager._admitted(handle)
                digest = self.project.blob_store.put_path(
                    blob.path, expected_digest=expected_blob["blob_hash"]
                )
                if digest != expected_blob["blob_hash"]:
                    raise RowError(
                        "invalid_output", "Blob storage returned different file bytes."
                    )


class _BoundRowFileStager:
    def __init__(self, owner, row_id):
        self._owner, self.row_id = owner, row_id

    def record_observation(self, facts):
        import uuid

        self._owner._stager._require_open()
        self._owner._observations.setdefault(self.row_id, []).append(
            {**copy.deepcopy(facts), "call_id": uuid.uuid4().hex}
        )

    def stage_output(self, output: RowBlobOutput, *, image=False, media_type=None):
        return self._owner._stage(self.row_id, output, image, media_type)

    def dump_field(self, annotation, value, key):
        annotation = _declared_annotation(annotation, value)
        if isinstance(value, Outcome):
            result = TypeAdapter(annotation).dump_python(
                value, mode="python", exclude={"value"}
            )
            if value.status != "failed":
                result["value"] = self._dump(
                    annotation.model_fields["value"].annotation, value.value, key, ()
                )
            return result
        return self._dump(annotation, value, key, ())

    def _dump(self, annotation, value, key, path):
        from frisket.actions.core import _has_staged_file_annotation, _table_output_key

        if not _has_staged_file_annotation(annotation):
            return TypeAdapter(annotation).dump_python(
                value, mode="json", by_alias=True
            )
        if value is None:
            return None
        annotation = _declared_annotation(annotation, value)
        if isinstance(annotation, type) and issubclass(annotation, StagedFile):
            if not isinstance(value, annotation):
                raise RowError(
                    "invalid_output", "File output is not an admitted handle."
                )
            return self._owner._lower_file(self.row_id, key, path, value)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if annotation.__pydantic_root_model__:
                return self._dump(
                    annotation.model_fields["root"].annotation, value.root, key, path
                )
            return {
                _table_output_key(name, info): self._dump(
                    info.annotation,
                    getattr(value, name),
                    key,
                    (*path, _table_output_key(name, info)),
                )
                for name, info in annotation.model_fields.items()
            }
        args, origin = get_args(annotation), get_origin(annotation)
        if isinstance(value, (list, tuple)):
            return [
                self._dump(
                    args[index]
                    if origin is tuple and args[-1] is not Ellipsis
                    else args[0],
                    item,
                    key,
                    (*path, index),
                )
                for index, item in enumerate(value)
            ]
        if isinstance(value, dict):
            return {
                name: self._dump(args[1], item, key, (*path, name))
                for name, item in value.items()
            }
        raise RowError("invalid_output", "Unsupported staged output position.")

    def descriptors(self, key):
        return [
            copy.deepcopy(descriptor)
            for descriptor, _ in self._owner._pending.get(self.row_id, [])
            if descriptor["output_key"] == key
        ]


def _declared_annotation(annotation, value):
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) in (types.UnionType, Union):
        for option in get_args(annotation):
            try:
                TypeAdapter(option).validate_python(value)
            except ValueError:
                continue
            return _declared_annotation(option, value)
        raise RowError("invalid_output", "Output does not match its declared union.")
    return annotation


def _declared_file_paths(annotation, value, path=()):
    from frisket.actions.core import _has_staged_file_annotation, _table_output_key

    if value is None or not _has_staged_file_annotation(annotation):
        return set()
    while get_origin(annotation) is Annotated:
        annotation = get_args(annotation)[0]
    if get_origin(annotation) in (types.UnionType, Union):
        choices = [
            choice for choice in get_args(annotation) if choice is not type(None)
        ]
        if len(choices) != 1:
            raise ValueError("ambiguous returned staged-file union")
        return _declared_file_paths(choices[0], value, path)
    if isinstance(annotation, type) and issubclass(annotation, StagedFile):
        return {path}
    if isinstance(annotation, type) and issubclass(annotation, Outcome):
        return _declared_file_paths(
            annotation.model_fields["value"].annotation, value, path
        )
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation.__pydantic_root_model__:
            return _declared_file_paths(
                annotation.model_fields["root"].annotation, value, path
            )
        return set().union(
            *(
                _declared_file_paths(
                    info.annotation,
                    value[_table_output_key(name, info)],
                    (*path, _table_output_key(name, info)),
                )
                for name, info in annotation.model_fields.items()
            )
        )
    args, origin = get_args(annotation), get_origin(annotation)
    if isinstance(value, list):
        return set().union(
            *(
                _declared_file_paths(
                    args[index]
                    if origin is tuple and args[-1] is not Ellipsis
                    else args[0],
                    item,
                    (*path, index),
                )
                for index, item in enumerate(value)
            )
        )
    if isinstance(value, dict):
        return set().union(
            *(
                _declared_file_paths(args[1], item, (*path, name))
                for name, item in value.items()
            )
        )
    raise ValueError("invalid returned staged-file shape")


def verify_row_files(project, results, *, row_id, fields, output_names):
    for field in fields:
        payload = results[output_names[field.key]]
        descriptors = list(row_file_descriptors({"result": payload}))
        expected = (
            set()
            if payload.get("error") is not None
            else _declared_file_paths(field.annotation, payload.get("value"))
        )
        if {tuple(item.item_path) for item in descriptors} != expected or len(
            descriptors
        ) != len(expected):
            raise ValueError("returned file paths differ from their declared schema")
        for descriptor in descriptors:
            if descriptor.row_id != row_id or descriptor.output_key != field.key:
                raise ValueError("returned file output association changed")
            for blob in (descriptor.primary, *descriptor.supplemental):
                with project.materialize_blob(blob.blob_hash) as path:
                    digest, size = sha256_blob_path(path)
                if digest != blob.blob_hash or size != blob.size:
                    raise ValueError("returned file bytes are missing or corrupt")


def write_row_file_evidence(
    project,
    *,
    batch,
    run_id,
    claim_token,
    writer_attempt_id,
    action_kind,
    output_columns,
):
    from frisket.engine.store.receipts import ReceiptStore
    from frisket.engine.store.row_files import record_row_file_calls

    op_id = project.db.execute(
        "SELECT op_id FROM runs WHERE id=?", (run_id,)
    ).fetchone()[0]
    record_row_file_calls(
        project,
        batch,
        run_id=run_id,
        writer_attempt_id=writer_attempt_id,
        claim_token=claim_token,
    )
    for result in batch:
        if result.get("error") is not None:
            continue
        register_returned_row_files(project, {"result": result})
        for descriptor in row_file_descriptors({"result": result}):
            value = result["value"]
            for part in descriptor.item_path:
                value = value[part]
            expected = media_cell(
                descriptor.primary.blob_hash,
                mime=descriptor.primary.mime,
                filename=descriptor.primary.filename,
            )
            if (
                value != expected
                or descriptor.row_id != result["row_id"]
                or output_columns.get(descriptor.output_key) != result["column_id"]
            ):
                raise ValueError("row file occurrence differs from accepted output")
            data = descriptor.model_dump(mode="json")
            kind = descriptor.facts.get("kind")
            kwargs = dict(
                row_id=result["row_id"],
                column_id=result["column_id"],
                output_key=descriptor.output_key,
                item_path=descriptor.item_path,
                run_id=run_id,
                op_id=op_id,
            )
            publication = None
            if kind == "screenshot":
                from frisket.engine.executor.screenshot_read import (
                    publish_row_file_evidence,
                )

                publication = publish_row_file_evidence(project, data, **kwargs)
            elif kind == "media_download":
                from frisket.engine.executor.media_download import (
                    publish_download_channel_facts,
                )

                publish_download_channel_facts(project, data, **kwargs)
            elif kind not in {"fetch", "video_frame", "face_crop", "searchable_pdf"}:
                raise ValueError("unrecognized row file acquisition")
            ReceiptStore(project)._record_writer_evidence(
                {
                    "kind": "row_file_output",
                    "action_kind": action_kind,
                    "column_id": result["column_id"],
                    "value_hash": canonical_json_hash(result["value"]),
                    "publication": publication,
                    **_file_feed_metadata(descriptor.primary),
                    **data,
                },
                run_id=run_id,
                writer_attempt_id=writer_attempt_id,
                claim_token=claim_token,
            )


def _file_feed_metadata(blob):
    import mimetypes
    from frisket.engine.store.media_blobs import MEDIA_ACQUISITION_NAMESPACE

    mime = blob.mime.lower()
    if mime in {"application/octet-stream", "binary/octet-stream", ""}:
        mime = mimetypes.guess_type(blob.filename)[0] or mime
    kind = next(
        (kind for kind in ("audio", "video", "image") if mime.startswith(kind + "/")),
        "file",
    )
    next_action = {
        "audio": "media.transcribe",
        "video": "media.video_frames",
        "image": "media.ocr",
        "file": "media.to_markdown",
    }[kind]
    acquisition = blob.metadata.get(MEDIA_ACQUISITION_NAMESPACE, {})
    return {
        "media_kind": kind,
        "may_feed": [next_action],
        "duration_seconds": acquisition.get("duration_seconds"),
        "managed_runtime": acquisition.get("managed_runtime"),
    }


def verify_receipt_row_files(project, receipt):
    """Verify recorded occurrences without reopening an acquisition capability."""
    from frisket.engine.store.media_blobs import MediaBlobStore

    for evidence in receipt.evidence:
        observed = evidence.ref
        if observed.get("kind") != "row_file_output":
            continue
        descriptor = RowFileOccurrence.model_validate(
            {name: observed[name] for name in RowFileOccurrence.model_fields}
        )
        for blob in (descriptor.primary, *descriptor.supplemental):
            if MediaBlobStore(project).blob_row(blob.blob_hash) is None:
                raise ValueError("recorded file registration is missing")
            with project.materialize_blob(blob.blob_hash) as path:
                digest, size = sha256_blob_path(path)
            if digest != blob.blob_hash or size != blob.size:
                raise ValueError("recorded file bytes are missing or corrupt")
        if descriptor.facts.get("kind") == "screenshot":
            from frisket.engine.executor.screenshot_read import verify_row_file_evidence

            verify_row_file_evidence(
                project, descriptor.model_dump(mode="json"), observed.get("publication")
            )
