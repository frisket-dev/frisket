"""resolve_media_blob_inputs: the shared SDK per-row blob validator.

Typed media actions have their own semantic-source/admitted-reader tests;
this suite exercises the still-supported SDK helper without a retired Op.

The load-bearing behavior here is the empty-cell split: a media column
routinely has rows whose download failed or never ran, and those rows are
SCOPE (skipped), not a launch veto — while a present-but-unmaterialized
value (a raw URL string) still refuses, and an entirely blob-less target
still gets a clear error instead of an empty run."""

from __future__ import annotations

from frisket.engine.store.media_blobs import owned_media_metadata_document

from types import SimpleNamespace

from frisket.contracts.action import ActionError
from frisket.engine.store import Project
from frisket.engine.store.media_blobs import media_cell
from frisket.sdk.media import resolve_media_blob_inputs
from frisket.sdk.inputs import Columns


def _resolve(project: Project, sheet_id: int, row_ids=None):
    params = SimpleNamespace(
        sheet_id=sheet_id, input_columns=["media"], row_ids=row_ids
    )
    return resolve_media_blob_inputs(
        project,
        params,
        action_kind="media.transcribe",
        selected=Columns(
            "input_columns", min=1, max=1, types=("audio", "video", "file")
        ).select(params),
        blob_kind="media_transcribe_blob_input",
        missing_blob_message="materialize URLs first",
    )


def _seed(tmp_path, cells: list):
    project = Project.create(tmp_path / "p.frisket", name="blob inputs")
    sheet_id = project.add_sheet("Episodes")
    cols = {"media": project.add_column(sheet_id, "media", type="audio")}
    row_ids = project.add_rows(sheet_id, [{"media": cell} for cell in cells], cols)
    return project, sheet_id, row_ids


def _blob_cell(project: Project, label: str):
    digest = project.add_blob(
        b"RIFF0000WAVEfmt " + label.encode("ascii"),
        filename=f"{label}.wav",
        mime="audio/wav",
        source_url=f"https://cdn.example/{label}.wav",
        metadata=owned_media_metadata_document(
            probe={"duration_seconds": 0.5, "kind": "audio"}
        ),
    )
    return media_cell(digest, mime="audio/wav", filename=f"{label}.wav")


def test_empty_cells_are_skipped_not_a_launch_veto(tmp_path) -> None:
    project = Project.create(tmp_path / "p.frisket", name="blob inputs")
    sheet_id = project.add_sheet("Episodes")
    cols = {"media": project.add_column(sheet_id, "media", type="audio")}
    first = _blob_cell(project, "ep1")
    third = _blob_cell(project, "ep3")
    row_ids = project.add_rows(
        sheet_id,
        [{"media": first}, {"media": None}, {"media": third}],
        cols,
    )

    resolved = _resolve(project, sheet_id)
    assert not isinstance(resolved, ActionError)
    assert resolved["row_ids"] == [row_ids[0], row_ids[2]]
    assert [ref["row_id"] for ref in resolved["blob_refs"]] == [
        row_ids[0],
        row_ids[2],
    ]


def test_a_raw_url_string_cell_still_refuses(tmp_path) -> None:
    project, sheet_id, row_ids = _seed(tmp_path, ["https://cdn.example/raw.mp3"])
    error = _resolve(project, sheet_id)
    assert isinstance(error, ActionError)
    assert error.code == "invalid_input_ref"
    assert error.message == "materialize URLs first"
    assert error.details == {"row_id": row_ids[0], "column": "media"}


def test_all_empty_target_rows_get_a_clear_error(tmp_path) -> None:
    project, sheet_id, _row_ids = _seed(tmp_path, [None, None])
    error = _resolve(project, sheet_id)
    assert isinstance(error, ActionError)
    assert error.code == "invalid_input_ref"
    assert "no blob-backed media cells" in error.message
