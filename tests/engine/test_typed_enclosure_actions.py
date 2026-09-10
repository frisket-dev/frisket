from __future__ import annotations

import pytest
from contextlib import closing

from frisket.actions.core import ActionCategory, ActionNamespace, ActionRegistry, action
from frisket.actions.enclosure_types import (
    EnclosureMaterializer,
    MaterializedEnclosures,
)
from frisket.actions.system import BoundTypedActionRequest, validate_root_action
from frisket.actions.types import ActionParams, ActionRequest, SheetRows
from frisket.engine.executor.enclosure_action import run_typed_enclosure_action
from frisket.engine.store import Project
from tests.engine.test_media_enclosure_materialize_executor import (
    PROJECT_ID,
    _cell,
    _columns,
    _fetch,
    _media_enclosure_action,
    _receipt,
    _run,
    _seed_enclosure_sheet,
)


class KeepParams(ActionParams):
    keep_existing: bool = True


def custom_materialize(
    params: KeepParams, download: EnclosureMaterializer
) -> MaterializedEnclosures:
    download.materialize(force=not params.keep_existing)
    # The domain return does not grant authority over the host's recorded effects.
    return MaterializedEnclosures(rows=())


def test_two_custom_actions_reuse_capability_and_actual_arguments(tmp_path):
    registry = ActionRegistry(
        (
            ActionNamespace(
                "custom",
                actions=tuple(
                    action(
                        name=name,
                        title=name,
                        description=name,
                        category=ActionCategory.CONVERT,
                        run=custom_materialize,
                    )
                    for name in ("one", "two")
                ),
            ),
        )
    )
    with closing(Project.create(tmp_path / "custom.frisket", name="Custom")) as project:
        sheet, rows = _seed_enclosure_sheet(project, urls=["https://cdn.example/a.mp3"])
        calls = []

        def fetch(url):
            calls.append(url)
            return f"audio{len(calls)}".encode(), "audio/mpeg", "a.mp3", None

        for index, (name, keep) in enumerate(
            (("one", True), ("two", True), ("two", False))
        ):
            request = ActionRequest(
                action_id=f"custom.{name}",
                scope=SheetRows(sheet_id=sheet, row_ids=rows),
                params={"keep_existing": keep},
                idempotency_key=f"custom:{index}",
            )
            bound = BoundTypedActionRequest.bind(
                registry.get(request.action_id), request
            )
            result = run_typed_enclosure_action(
                project, PROJECT_ID, bound, enclosure_fetcher=fetch
            )
            assert result.status == "completed"
            receipt = _receipt(project, result.receipt_id)
            assert receipt.action_kind == request.action_id
            assert any(output.ref["kind"] == "media_blob" for output in receipt.outputs)
            assert bool(receipt.provider_use) == (index != 1)
        assert (
            len(calls) == 2
        )  # Renamed and derived force argument, not builtin Params lookup.


@pytest.mark.parametrize("selection", [None, [], list(range(1, 502))])
def test_enclosures_require_explicit_bounded_scope(selection):
    request = _media_enclosure_action(sheet_id=1, row_ids=[1])
    request["scope"]["row_ids"] = selection
    assert not validate_root_action(request).ok


@pytest.mark.parametrize("value", [1, "true", None])
def test_force_rejects_non_booleans(value):
    request = _media_enclosure_action(sheet_id=1, row_ids=[1])
    request["params"]["force"] = value
    assert not validate_root_action(request).ok


def test_enclosure_preview_refuses_before_download_or_writes(tmp_path, monkeypatch):
    from frisket.engine.executor.actions import resolve_map_preview

    monkeypatch.setattr(
        "frisket.ops.enclosures.download_url",
        lambda *a, **k: pytest.fail("preview fetched"),
    )
    with closing(
        Project.create(tmp_path / "preview.frisket", name="Preview")
    ) as project:
        sheet, rows = _seed_enclosure_sheet(project)
        columns = len(project.columns(sheet))
        error = resolve_map_preview(
            project, _media_enclosure_action(sheet_id=sheet, row_ids=rows)
        )
        assert error.code == "unsupported_action_kind"
        assert len(project.columns(sheet)) == columns
        assert project.db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_changed_next_url_stops_before_fetch_and_keeps_prior_commit(tmp_path):
    with closing(
        Project.create(tmp_path / "changed.frisket", name="Changed")
    ) as project:
        sheet, rows = _seed_enclosure_sheet(project)
        calls = []

        def fetch(url):
            calls.append(url)
            project.apply_edits(
                [
                    {
                        "row_id": rows[1],
                        "column_id": _columns(project, sheet)["enclosure_url"],
                        "value": "https://changed.example/new.mp3",
                    }
                ],
                label="change another enclosure",
            )
            return _fetch(url)

        request = _media_enclosure_action(sheet_id=sheet, row_ids=rows)
        result = _run(project, request, fetch)
        assert result.status == "partial"
        assert len(calls) == 1
        assert _cell(project, sheet, rows[0], "media")["blob"]
        assert _cell(project, sheet, rows[1], "media") is None
        receipt = _receipt(project, result.receipt_id)
        assert receipt.status == "partial" and receipt.op_ids
        assert receipt.errors[0].code == "invalid_input_ref"
        replay = _run(project, request, lambda _: pytest.fail("replay fetched"))
        assert replay.receipt_id == result.receipt_id


@pytest.mark.parametrize("mutation_phase", ["download", "probe"])
def test_changed_current_url_is_not_published_after_download(
    tmp_path, monkeypatch, mutation_phase
):
    with closing(
        Project.create(tmp_path / "changed-current.frisket", name="Changed")
    ) as project:
        sheet, rows = _seed_enclosure_sheet(project, urls=["https://cdn.example/a.mp3"])

        def mutate_source():
            project.apply_edits(
                [
                    {
                        "row_id": rows[0],
                        "column_id": _columns(project, sheet)["enclosure_url"],
                        "value": "https://changed.example/new.mp3",
                    }
                ],
                label="change downloading enclosure",
            )

        def fetch(url):
            if mutation_phase == "download":
                mutate_source()
            return _fetch(url)

        if mutation_phase == "probe":
            from frisket.ops import enclosures

            probe = enclosures.probe_for_ingest

            def mutate_then_probe(*args, **kwargs):
                mutate_source()
                return probe(*args, **kwargs)

            monkeypatch.setattr(enclosures, "probe_for_ingest", mutate_then_probe)

        result = _run(
            project, _media_enclosure_action(sheet_id=sheet, row_ids=rows), fetch
        )
        assert result.status == "failed"
        assert _cell(project, sheet, rows[0], "media") is None
        receipt = _receipt(project, result.receipt_id)
        assert (
            receipt.provider_use
        )  # Actual fetch was attempted even though publication refused.
        assert not any(output.ref["kind"] == "media_blob" for output in receipt.outputs)
