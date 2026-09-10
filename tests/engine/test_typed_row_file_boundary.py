"""Staged row-file ownership, declared shapes, and publication boundaries."""

import hashlib
from typing import Annotated

import pytest
from PIL import Image
from pydantic import BaseModel, Field, field_serializer

from frisket.actions.core import ActionCategory, RegisteredAction, action, map_rows
from frisket.actions.file_types import FileFetcher
from frisket.actions.system import BoundTypedActionRequest
from frisket.actions.types import (
    ActionParams,
    ActionRequest,
    ColumnRef,
    Outcome,
    Row,
    RowError,
    RowResult,
    StagedFile,
    StagedImage,
)
from frisket.ai.llm import ModelRouter
from frisket.engine.executor.actions import _default_map_runner_factory
from frisket.engine.executor.blob_outputs import RowBlobOutput, RowBlobPlan
from frisket.engine.executor.map_rows_action import run_typed_map_rows_action
from frisket.engine.executor.row_file_stage import RowFileStager
from frisket.engine.store import Project
from frisket.engine.store.receipts import ReceiptStore


@pytest.fixture
def staged(tmp_path):
    owner = RowFileStager(None)
    bound = owner.bind_row(1)
    counter = 0

    def issue(*, payload=None, mime="image/png", image=True):
        nonlocal counter
        counter += 1
        path = tmp_path / f"asset-{counter}"
        if payload is None:
            Image.new("RGB", (2, 2), "red").save(path, format="PNG")
        else:
            path.write_bytes(payload)
        return bound.stage_output(
            RowBlobOutput(
                primary=RowBlobPlan(
                    role="download",
                    content_digest=hashlib.sha256(path.read_bytes()).hexdigest(),
                    staged_path=path,
                    filename="asset.png",
                    mime=mime,
                    source_url=None,
                ),
                facts={"kind": "fetch"},
            ),
            image=image,
        )

    try:
        yield owner, bound, issue
    finally:
        owner.close()


@pytest.mark.parametrize(
    "payload,mime", [(b"not an image", "image/png"), (None, "image/jpeg")]
)
def test_image_must_match_actual_verified_bytes(staged, payload, mime):
    _, _, issue = staged
    with pytest.raises(RowError):
        issue(payload=payload, mime=mime)


def test_declared_model_excludes_subclass_staged_fields(staged):
    _, bound, issue = staged

    class Declared(BaseModel):
        image: StagedImage

    class Extra(Declared):
        secret: StagedFile

    value = Extra(
        image=issue(), secret=issue(payload=b"secret", mime="text/plain", image=False)
    )
    lowered = bound.dump_field(Declared, value, "pictures")
    assert set(lowered) == {"image"}
    assert [item["item_path"] for item in bound.descriptors("pictures")] == [["image"]]


def test_wrapped_fixed_tuple_and_outcome_use_declared_paths(staged):
    _, bound, issue = staged

    class Nested(BaseModel):
        pair: tuple[Annotated[StagedImage, Field(description="picture")], int]

    handle = issue()
    value = Nested(pair=(handle, 7))
    lowered = bound.dump_field(Outcome[Nested], Outcome[Nested].ok(value), "wrapped")
    assert lowered["value"]["pair"][1] == 7
    assert [item["item_path"] for item in bound.descriptors("wrapped")] == [["pair", 0]]


def test_duplicate_occurrences_remain_distinct_and_foreign_handles_refuse(staged):
    owner, bound, issue = staged
    handle = issue()
    values = bound.dump_field(list[StagedImage], [handle, handle], "images")
    assert values[0] == values[1]
    assert [item["item_path"] for item in bound.descriptors("images")] == [[0], [1]]
    with pytest.raises(RowError):
        owner.bind_row(2).dump_field(StagedImage, handle, "foreign")
    with pytest.raises(RowError):
        bound.dump_field(StagedImage, StagedImage(handle.size), "forged")


def test_nested_alias_lowering_matches_declared_schema(staged):
    _, bound, issue = staged

    class Item(BaseModel):
        image: StagedImage = Field(alias="photo")

    class Output(BaseModel):
        item: Item

    def handler(params: ActionParams, row: Row) -> RowResult[Output]:
        raise AssertionError

    map_rows(handler)
    value = Item(photo=issue())
    lowered = bound.dump_field(Item, value, "pictures")
    assert set(Item.model_json_schema()["required"]) <= set(lowered)
    assert [item["item_path"] for item in bound.descriptors("pictures")] == [["photo"]]


def test_staged_subtree_serializer_refuses_registration():
    class Item(BaseModel):
        image: StagedImage

        @field_serializer("image")
        def replace(self, value):
            return {"blob": "forged"}

    class Output(BaseModel):
        items: list[Item]

    def handler(params: ActionParams, row: Row) -> RowResult[Output]:
        raise AssertionError

    with pytest.raises(TypeError, match="serializers"):
        map_rows(handler)


class FetchParams(ActionParams):
    document: ColumnRef[str]
    partial: bool = False


class FetchOutput(BaseModel):
    first: StagedFile
    second: Outcome[StagedFile]


async def duplicate_fetch(
    params: FetchParams, row: Row, fetcher: FileFetcher
) -> RowResult[FetchOutput]:
    handle = await fetcher.fetch(row.values[params.document.name])
    second = (
        Outcome[StagedFile].failed("optional_failed", "Optional file failed")
        if params.partial
        else Outcome[StagedFile].ok(handle)
    )
    return RowResult(output=FetchOutput(first=handle, second=second))


@pytest.fixture
def fetch_run(tmp_path, monkeypatch):
    calls = []

    def download(url, **kwargs):
        calls.append(url)
        return b"same downloaded bytes", "text/plain", "f.txt", None

    monkeypatch.setattr("frisket.ops.enclosures.download_url", download)
    project = Project.create(tmp_path / "custom-fetch.frisket")
    sheet = project.add_sheet("Data")
    column = project.add_column(sheet, "url", "text")
    rows = project.add_rows(
        sheet, [{"url": "https://example.test/one"}], {"url": column}
    )
    registered = RegisteredAction(
        "custom.review_fetch",
        action(
            name="review_fetch",
            title="Review",
            description="Independent probe",
            category=ActionCategory.EXTRACT,
            run=map_rows(duplicate_fetch),
        ),
    )
    request = ActionRequest(
        action_id=registered.action_id,
        scope={"kind": "sheet_rows", "sheet_id": sheet},
        params={"document": "url"},
        output_names={"first": "One", "second": "Two"},
        idempotency_key="review-fetch",
    )
    router = ModelRouter()

    def run(*, partial=False):
        nonlocal request
        if partial:
            request = request.model_copy(
                update={"params": {"document": "url", "partial": True}}
            )
        bound = BoundTypedActionRequest.bind(registered, request)
        result = run_typed_map_rows_action(
            project, "review", bound, router, _default_map_runner_factory
        )
        if result.status == "needs_confirmation":
            request = request.model_copy(
                update={"confirmation": result.errors[0].details["promise_set_hash"]}
            )
            result = run_typed_map_rows_action(
                project,
                "review",
                BoundTypedActionRequest.bind(registered, request),
                router,
                _default_map_runner_factory,
            )
        return result

    try:
        yield project, rows, calls, run
    finally:
        project.close()


def test_real_two_output_outcome_publication_has_two_exact_occurrences(fetch_run):
    project, rows, calls, run = fetch_run
    result = run()
    assert result.status == "completed", result.model_dump(mode="json")
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    files = [
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "row_file_output"
    ]
    assert len(files) == 2 and {item["output_key"] for item in files} == {
        "first",
        "second",
    }
    assert len({item["column_id"] for item in files}) == 2
    assert all(item["row_id"] == rows[0] and item["item_path"] == [] for item in files)
    assert len(calls) == 1
    assert run().receipt_id == result.receipt_id and len(calls) == 1


def test_published_supplemental_file_is_rooted_by_retained_receipt(
    fetch_run, tmp_path, monkeypatch
):
    from dataclasses import replace

    project, _, calls, run = fetch_run
    sidecar = tmp_path / "download.vtt"
    sidecar.write_bytes(b"WEBVTT\n\n00:00.000 --> 00:01.000\nA caption\n")
    digest = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    stage = RowFileStager._stage

    def with_supplemental(self, row_id, output, image, media_type=None):
        # The acquisition host issues the supplemental descriptor, not the author.
        output = replace(
            output,
            supplemental=(
                RowBlobPlan(
                    role="captions",
                    content_digest=digest,
                    staged_path=sidecar,
                    filename=sidecar.name,
                    mime="text/vtt",
                    source_url=None,
                ),
            ),
        )
        return stage(self, row_id, output, image, media_type)

    monkeypatch.setattr(RowFileStager, "_stage", with_supplemental)
    result = run()
    assert result.status == "completed", result.errors
    assert (
        project.db.execute("SELECT COUNT(*) FROM effect_checkpoints").fetchone()[0] == 0
    )
    assert all(
        digest not in row[0]
        for row in project.db.execute(
            "SELECT value FROM results WHERE value IS NOT NULL"
        )
    )
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    files = [
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "row_file_output"
    ]
    assert len(files) == 2
    assert all(item["supplemental"][0]["blob_hash"] == digest for item in files)
    orphan = project.add_blob(b"unreferenced", filename="orphan.txt", mime="text/plain")
    collected = project.gc_blobs()
    assert digest not in collected["hashes"]
    assert orphan in collected["hashes"]
    assert (
        project.db.execute("SELECT hash FROM blobs WHERE hash=?", (digest,)).fetchone()
        is not None
    )
    with project.materialize_blob(digest) as stored:
        assert stored.read_bytes() == sidecar.read_bytes()
    assert run().receipt_id == result.receipt_id and len(calls) == 1


def test_failed_optional_file_preserves_other_accepted_output(fetch_run):
    project, _, calls, run = fetch_run
    result = run(partial=True)
    # A failed optional output does not erase the independently accepted sibling;
    # the receipt reports the mixed result as partial, not total failure.
    assert result.status == "partial", result.model_dump(mode="json")
    receipt = ReceiptStore(project).parsed_by_id(result.receipt_id)
    files = [
        item.ref
        for item in receipt.evidence
        if item.ref.get("kind") == "row_file_output"
    ]
    assert len(files) == 1 and files[0]["output_key"] == "first"
    assert len(calls) == 1


class InjectedDeath(BaseException):
    pass


def test_late_file_evidence_failure_rolls_back_both_output_results(
    fetch_run, monkeypatch
):
    project, _, _, run = fetch_run
    original = ReceiptStore._record_writer_evidence
    seen = []

    def die(self, fact, **kwargs):
        original(self, fact, **kwargs)
        if fact.get("kind") == "row_file_output":
            seen.append(fact)
            if len(seen) == 2:
                raise InjectedDeath

    monkeypatch.setattr(ReceiptStore, "_record_writer_evidence", die)
    with pytest.raises(InjectedDeath):
        run()
    assert len(seen) == 2
    assert project.db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 0
    for stored in project.db.execute("SELECT id FROM receipts"):
        receipt = ReceiptStore(project).parsed_by_id(stored[0])
        assert not any(
            item.ref.get("kind") == "row_file_output" for item in receipt.evidence
        )
