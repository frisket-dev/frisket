"""Atomic FollowTheMoney dataset imports and retained project-blob exports."""

from __future__ import annotations

import hashlib
import io
import zipfile
from typing import Any

from pydantic import TypeAdapter, ValidationError

from frisket.contracts.plugin_write_plan import MAX_TOTAL_CELLS
from frisket.actions.entity_package_types import (
    EntityRowset,
    ExportedEntityPackage,
    ImportedEntityDataset,
    ImportedEntitySheet,
)
from frisket.actions.types import TableError
from frisket.contracts.action import ActionError, ReceiptEvidence, ReceiptIO
from frisket.engine.executor.import_sources import open_local_file_reader
from frisket.engine.executor.plugin_write_apply import (
    apply_additive_tables,
    WritePlanValidationError,
)
from frisket.engine.store.blob_backend import BlobIntegrityError, BlobNotFoundError
from frisket.engine.store.project_blobs import add_blob
from frisket.features.followthemoney import entities_available
from frisket.features.investigations.rowsets import (
    resolve_investigative_rowset,
    InvestigativeRowsetError,
)


# Match the existing table-warning retention scale. The last slot becomes an
# explicit truncation summary when a source produces more diagnostics.
MAX_RETAINED_IMPORT_DIAGNOSTICS = 100
MAX_RETAINED_IMPORT_DIAGNOSTIC_CHARS = 2000


def _refuse(code, message):
    from frisket.engine.executor.callable_action import CallablePrimitiveError

    raise CallablePrimitiveError(ActionError(code=code, message=message))


def _require_entities():
    available, error = entities_available()
    if not available:
        _refuse(
            "followthemoney_unavailable", error or "Install frisket-data[entities]."
        )


class FollowTheMoneyImportCapability:
    def __init__(self, invocation):
        self.invocation = invocation

    def import_entities(
        self, *, source_path: str, dataset_name: str | None = None
    ) -> ImportedEntityDataset:
        invocation = self.invocation
        invocation.context.check_cancelled()
        _require_entities()
        if dataset_name is not None and (
            not isinstance(dataset_name, str) or not dataset_name.strip()
        ):
            _refuse(
                "invalid_ftm_import", "dataset_name must be non-empty text or null."
            )
        from frisket.features.followthemoney.import_planner import (
            FollowTheMoneyImportLimitError,
            plan_followthemoney_import,
        )
        from frisket.features.followthemoney.write_plan import (
            import_plan_to_write_plan,
        )

        with open_local_file_reader(invocation.deps.local_file_sources) as files:
            try:
                host_max_rows = (
                    invocation.deps.import_workload_limits.max_rows
                    if invocation.deps.import_workload_limits is not None
                    else None
                )
                planning_max_rows = (
                    min(host_max_rows, MAX_TOTAL_CELLS)
                    if host_max_rows is not None
                    else MAX_TOTAL_CELLS
                )
                with files.open_text(source_path, encoding="utf-8") as source:
                    plan = plan_followthemoney_import(
                        source,
                        dataset_name=dataset_name,
                        max_entities=planning_max_rows,
                        max_diagnostics=MAX_RETAINED_IMPORT_DIAGNOSTICS,
                        max_diagnostic_chars=MAX_RETAINED_IMPORT_DIAGNOSTIC_CHARS,
                    )
            except FollowTheMoneyImportLimitError as error:
                if host_max_rows is not None and error.max_entities == host_max_rows:
                    _refuse(
                        "import_workload_limit_exceeded",
                        f"FtM import exceeds the {host_max_rows}-row host limit.",
                    )
                _refuse(
                    "invalid_ftm_import",
                    "FtM import exceeds the shared write-plan capacity.",
                )
            except UnicodeDecodeError:
                _refuse(
                    "invalid_ftm_import",
                    "FtM entity stream is not valid UTF-8.",
                )
            except TableError as error:
                _refuse(error.code, str(error))
            ordered_sheets = (
                *plan["sheets"],
                *plan["unsupported_sheets"],
                *plan["relationship_sheets"],
            )
            planned_rows = sum(sheet["row_count"] for sheet in ordered_sheets)
            if planned_rows == 0:
                _refuse(
                    "invalid_ftm_import",
                    "FtM source contained no valid importable entities.",
                )
            if host_max_rows is not None and planned_rows > host_max_rows:
                _refuse(
                    "import_workload_limit_exceeded",
                    f"FtM import exceeds the {host_max_rows}-row host limit.",
                )
            try:
                write_plan = import_plan_to_write_plan(plan)
            except ValidationError as error:
                _refuse("invalid_ftm_import", str(error))
            inputs = [ReceiptIO(name="source", ref=fact) for fact in files.facts]
        invocation.context.check_cancelled()
        project = invocation.project
        project.db.execute("BEGIN IMMEDIATE")
        try:
            applied = apply_additive_tables(
                project,
                write_plan,
                operation_kind="import.followthemoney",
                label="Import FollowTheMoney",
                provenance={
                    "action_kind": invocation.action.kind,
                    "receipt_id": invocation.receipt.receipt_id,
                    "source": inputs[0].ref,
                    "dataset_name": dataset_name,
                },
            )
            sheets = []
            outputs = []
            for index, sheet in enumerate(
                (
                    *plan["sheets"],
                    *plan["unsupported_sheets"],
                    *plan["relationship_sheets"],
                )
            ):
                ref = f"s{index}"
                imported = ImportedEntitySheet(
                    name=sheet["sheet_name"],
                    schema_name=sheet["schema"],
                    kind=sheet["kind"],
                    sheet_id=applied.sheet_ids[ref],
                    row_ids=tuple(applied.row_ids[ref]),
                    column_ids={
                        column["name"]: applied.column_ids[f"{ref}_c{n}"]
                        for n, column in enumerate(sheet["columns"])
                    },
                )
                sheets.append(imported)
                outputs.extend(
                    (
                        ReceiptIO(
                            name=imported.name,
                            ref={
                                "kind": "materialized_sheet",
                                "sheet_id": imported.sheet_id,
                            },
                        ),
                        ReceiptIO(
                            name=f"{imported.name}.rows",
                            ref={
                                "kind": "materialized_rows",
                                "sheet_id": imported.sheet_id,
                                "row_ids": list(imported.row_ids),
                            },
                        ),
                        *(
                            ReceiptIO(
                                name=name,
                                ref={
                                    "kind": "materialized_column",
                                    "sheet_id": imported.sheet_id,
                                    "column_id": column_id,
                                },
                            )
                            for name, column_id in imported.column_ids.items()
                        ),
                    )
                )
            result = ImportedEntityDataset(
                dataset_name=dataset_name,
                sheets=tuple(sheets),
                op_id=applied.op_id,
                diagnostics=tuple(plan["diagnostics"]),
            )
            invocation.context.check_cancelled()
            observed = invocation.record(
                inputs=inputs,
                outputs=outputs,
                op_ids=(applied.op_id,),
                evidence=(
                    ReceiptEvidence(
                        ref={
                            "kind": "entity_dataset_import",
                            "dataset": result.model_dump(mode="json"),
                        },
                        retention="materialized",
                    ),
                ),
                commit=False,
            )
            project.db.commit()
        except BaseException as error:
            project.db.rollback()
            if isinstance(error, WritePlanValidationError):
                _refuse("invalid_ftm_import", str(error))
            raise
        invocation.receipt = observed
        return result


class FollowTheMoneyExportCapability:
    def __init__(self, invocation):
        self.invocation = invocation

    def export_entities(
        self,
        *,
        rowsets: list[EntityRowset],
        mappings: list[dict[str, Any]],
        filename="entities.ftm.zip",
        validate=True,
    ) -> ExportedEntityPackage:
        invocation = self.invocation
        invocation.context.check_cancelled()
        _require_entities()
        try:
            selected = TypeAdapter(list[EntityRowset]).validate_python(rowsets)
            mappings = TypeAdapter(list[dict[str, Any]]).validate_python(
                mappings, strict=True
            )
            if not selected or len(
                {(item.kind, item.sheet_id) for item in selected}
            ) != len(selected):
                raise ValueError("rowsets must be non-empty and unique")
            if (
                type(validate) is not bool
                or not isinstance(filename, str)
                or not filename.strip()
                or any(c in filename for c in ("/", "\\", "\r", "\n", "\0"))
                or filename in {".", ".."}
            ):
                raise ValueError("invalid package filename or validate flag")
        except (ValueError, ValidationError) as error:
            _refuse("invalid_ftm_export", str(error))
        from frisket.features.followthemoney.exporters import (
            build_followthemoney_export_package,
        )

        try:
            with invocation.project.read_snapshot() as project:
                resolved = {
                    f"{item.kind}:{item.sheet_id}": resolve_investigative_rowset(
                        project,
                        item.model_dump(),
                        project_id=invocation.receipt.project_id,
                    )
                    for item in selected
                }
        except InvestigativeRowsetError as error:
            _refuse(error.code, error.message)
        package = build_followthemoney_export_package(
            source={
                "kind": "selected_rowsets",
                "rowsets": [item.model_dump() for item in selected],
            },
            mappings=mappings,
            rowsets_by_key=resolved,
            project_id=invocation.receipt.project_id,
            media_policy="refs",
            validate=validate,
        )
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for artifact in package["artifacts"].values():
                info = zipfile.ZipInfo(
                    artifact["filename"], date_time=(1980, 1, 1, 0, 0, 0)
                )
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, artifact["content"])
        content = output.getvalue()
        digest = hashlib.sha256(content).hexdigest()
        result = ExportedEntityPackage(
            filename=filename,
            blob_hash=digest,
            byte_count=len(content),
            sha256="sha256:" + digest,
            entity_count=package["entity_count"],
            skipped_count=package["skipped_count"],
        )
        ref = result.model_dump(mode="json")

        def publish():
            invocation.context.check_cancelled()
            add_blob(
                invocation.project,
                content,
                filename=filename,
                mime="application/zip",
                commit=False,
            )

        invocation.record(
            inputs=tuple(
                ReceiptIO(
                    name=f"rowset.{key}",
                    ref={"kind": "investigative_rowset", "rowset": value["rowset"]},
                )
                for key, value in resolved.items()
            ),
            outputs=(ReceiptIO(name=filename, ref=ref),),
            exports=(ref,),
            evidence=(
                ReceiptEvidence(
                    ref={
                        "kind": "entity_package_export",
                        "blob_hash": digest,
                        "manifest": package["manifest"],
                    },
                    retention="pinned",
                ),
            ),
            before_commit=publish,
        )
        return result


def entity_effect_replay_error(project, receipt):
    for artifact in receipt.exports:
        if artifact.get("kind") != "export_project_file":
            continue
        digest = artifact.get("blob_hash")
        try:
            if (
                not isinstance(digest, str)
                or project.db.execute(
                    "SELECT 1 FROM blobs WHERE hash=?", (digest,)
                ).fetchone()
                is None
            ):
                raise BlobNotFoundError("project blob metadata is missing")
            content = project.read_blob(digest)
        except BlobIntegrityError:
            return ActionError(
                code="export_artifact_mismatch",
                message="Exported package bytes changed.",
                action_kind=receipt.action_kind,
            )
        except (BlobNotFoundError, OSError, ValueError):
            return ActionError(
                code="export_artifact_missing",
                message="Exported package is unavailable.",
                action_kind=receipt.action_kind,
            )
        if len(content) != artifact.get("byte_count") or "sha256:" + hashlib.sha256(
            content
        ).hexdigest() != artifact.get("sha256"):
            return ActionError(
                code="export_artifact_mismatch",
                message="Exported package bytes changed.",
                action_kind=receipt.action_kind,
            )
    for evidence in receipt.evidence:
        if evidence.ref.get("kind") != "entity_dataset_import":
            continue
        dataset = evidence.ref["dataset"]
        if project.db.execute(
            "SELECT 1 FROM ops WHERE id=? AND status='applied'", (dataset["op_id"],)
        ).fetchone() is None or any(
            project.db.execute(
                "SELECT 1 FROM sheets WHERE id=? AND hidden=0", (sheet["sheet_id"],)
            ).fetchone()
            is None
            for sheet in dataset["sheets"]
        ):
            return ActionError(
                code="stale_replay",
                message="Imported entity sheets are no longer active.",
                action_kind=receipt.action_kind,
            )
    return None
