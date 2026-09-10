"""Email rows and attachments produced from admitted, borrowed source streams."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel, Field

from frisket.actions.core import ActionCategory, action, create_sheet
from frisket.actions.types import (
    ActionParams,
    EmailSourceReader,
    EmailSourceRef,
    ImportBlobStager,
    StagedFile,
    TableError,
    TableResult,
    TableRow,
)
from frisket.ops import email_import


class EmailParams(ActionParams):
    sources: list[EmailSourceRef] = Field(min_length=1)


class EmailRow(BaseModel):
    date: str
    from_: str = Field(alias="from")
    to: list[str]
    cc: list[str]
    subject: str
    body: str = Field(json_schema_extra={"format": "plain_text"})
    html: str = Field(json_schema_extra={"format": "plain_text"})
    attachments: list[StagedFile]
    source_file: str


def import_email(
    params: EmailParams, sources: EmailSourceReader, blobs: ImportBlobStager
) -> TableResult[EmailRow]:
    warnings = email_import.EmailParseWarnings()

    def rows() -> Iterator[TableRow[EmailRow]]:
        found = False
        with sources.open(params.sources) as inputs:
            with TemporaryDirectory(prefix="frisket-email-attachments-") as scratch:
                parsed = email_import.iter_email_messages(
                    (
                        email_import.EmailSource(
                            path=None,
                            logical_path=source.logical_path,
                            format=source.format,
                            stream=source.stream,
                        )
                        for source in inputs
                    ),
                    attachment_dir=Path(scratch),
                    warnings=warnings,
                )
                with closing(parsed):
                    for message in parsed:
                        attachments = []
                        for attachment in message.attachments:
                            try:
                                with attachment.path.open("rb") as stream:
                                    attachments.append(
                                        blobs.stage(
                                            stream,
                                            filename=attachment.filename,
                                            mime=attachment.mime,
                                        )
                                    )
                            finally:
                                attachment.path.unlink(missing_ok=True)
                        values = message.row
                        values["attachments"] = attachments
                        found = True
                        yield TableRow(output=EmailRow.model_validate(values))
        if not found:
            raise TableError("email_parse_failed", "no valid email messages were found")

    def final_warnings() -> Iterator[str]:
        yield from warnings.values()

    return TableResult(
        rows=rows(),
        source={"kind": "file", "label": "email", "importer": "email"},
        warnings=final_warnings(),
    )


EMAIL = action(
    examples=(
        EmailParams(
            sources=[
                EmailSourceRef(
                    source_ref="example-upload", logical_path="inbox.eml", format="eml"
                )
            ]
        ),
    ),
    name="email",
    title="Import email",
    description="Import email messages with their headers, bodies, and attachments.",
    category=ActionCategory.CONVERT,
    run=create_sheet(import_email),
)
