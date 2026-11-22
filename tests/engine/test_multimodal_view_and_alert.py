"""Threshold-pinned local multimodal EmbeddingGemma 2 Views, Alerts, and cross-sheet blob dedup."""

from __future__ import annotations

import io
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter

import frisket.retrieval.preparation as preparation_mod
from frisket.ai.llm import ModelRouter
from frisket.alerts.service import check_alert
from frisket.engine.jobs import (
    ALERT_EVALUATE_KIND,
    FEED_POLL_KIND,
    SqliteJobQueue,
    Worker,
    register_alert_evaluate_handler,
    register_feed_poll_handler,
)
from frisket.engine.jobs.search_index import process_index_batches
from frisket.engine.store import Project
from frisket.engine.store.feeds import FeedStore
from frisket.engine.store.transactions import write_transaction
from frisket.engine.views.evaluate import open_selection
from frisket.engine.views.specs import SelectionUnavailable, normalize_selection
from frisket.execution.purchases import LOCAL_PURCHASE_POLICY
from frisket.retrieval.contracts import (
    RetrievalScope,
    SearchRequest,
    SheetScope,
    WeightedSemanticQuery,
    WeightedTerm,
)
from frisket.retrieval.encoding import (
    EmbeddingGemma2Space,
    ImageEncodingInput,
    TextEncodingInput,
    VideoClipEncodingInput,
)
from frisket.retrieval.media import (
    LocatedFrame,
    LocatedVideoClip,
    MediaCoverage,
    PresentationPoint,
    ProducedMedia,
)
from frisket.retrieval.preparation import (
    PDF_PAGE_CHANNEL,
    VIDEO_CHANNEL,
    preparation_work_marker,
    process_blob_image_preparation_batch,
    process_timed_media_preparation_batch,
)
from frisket.retrieval.query import RetrievalQueryService, SemanticQueryVectors
from frisket.retrieval.store import RetrievalStore
from frisket.search_index import index_batch
from frisket.server.app import create_app


def _unit_axis(dimensions: int, weights: dict[int, float]) -> tuple[float, ...]:
    vec = [0.0] * dimensions
    for idx, value in weights.items():
        vec[idx] = value
    norm = math.sqrt(sum(v * v for v in vec))
    assert norm > 0
    return tuple(v / norm for v in vec)


class _MultimodalTestEncoder:
    """Deterministic local encoder mapping text, images, PDFs, and video clips to 768-d unit vectors."""

    def __init__(self, *, is_available: bool = True) -> None:
        self._available = is_available
        self.calls = 0
        self.encoded_count = 0

    def available(self) -> bool:
        return self._available

    def _classify_image(self, image_bytes: bytes, dimensions: int) -> tuple[float, ...]:
        with Image.open(io.BytesIO(image_bytes)) as img:
            rgb = img.convert("RGB").getpixel((0, 0))
            assert isinstance(rgb, tuple)
            red, green, blue = int(rgb[0]), int(rgb[1]), int(rgb[2])
        if red >= green and red >= blue and red > 120:
            return _unit_axis(dimensions, {0: 1.0})
        if green >= red and green >= blue and green > 120:
            return _unit_axis(dimensions, {1: 1.0})
        return _unit_axis(dimensions, {2: 1.0})

    def _classify_text(self, text: str, dimensions: int) -> tuple[float, ...]:
        lowered = text.lower()
        if any(word in lowered for word in ("contract", "invoice", "procurement")):
            if "urgent" in lowered:
                return _unit_axis(dimensions, {0: 0.99, 1: 0.14})
            return _unit_axis(dimensions, {0: 0.9, 1: 0.43588989435406733})
        if any(word in lowered for word in ("wildlife", "forest", "bird")):
            return _unit_axis(dimensions, {1: 1.0})
        return _unit_axis(dimensions, {2: 1.0})

    def encode(self, inputs, *, purpose, space, cancelled=None):
        del purpose
        assert cancelled is None or not cancelled()
        self.calls += 1
        self.encoded_count += len(inputs)
        out: list[tuple[float, ...]] = []
        for item in inputs:
            if isinstance(item, TextEncodingInput):
                out.append(self._classify_text(item.text, space.dimensions))
            elif isinstance(item, ImageEncodingInput):
                out.append(self._classify_image(item.image_bytes, space.dimensions))
            elif isinstance(item, VideoClipEncodingInput):
                out.append(
                    self._classify_image(item.frames[0].image_bytes, space.dimensions)
                )
            else:
                out.append(_unit_axis(space.dimensions, {2: 1.0}))
        return out


def _png_bytes(color: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def _two_page_pdf_bytes() -> bytes:
    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_blank_page(width=144, height=72)
    writer.write(buf)
    return buf.getvalue()


def _drain_local(project, encoder) -> None:
    for _ in range(32):
        _processed, complete = process_index_batches(project, embedding_encoder=encoder)
        if complete:
            return
    raise AssertionError("local retrieval preparation did not converge")


def _drain_dirty(project) -> None:
    while not index_batch(project, batch_size=50).complete:
        pass


def _rss_feed(items: list[dict[str, str]]) -> str:
    entries = []
    for item in items:
        entries.append(
            "<item>"
            f"<guid isPermaLink='false'>{item['guid']}</guid>"
            f"<title>{item['title']}</title>"
            f"<link>{item['link']}</link>"
            f"<description>{item['description']}</description>"
            "</item>"
        )
    return (
        "<?xml version='1.0'?><rss version='2.0'><channel>"
        "<title>Filings</title>" + "".join(entries) + "</channel></rss>"
    )


def test_local_retrieval_view_text_query_sheet_and_project_scopes(
    make_project,
) -> None:
    project = make_project("local-retrieval-scopes")
    encoder = _MultimodalTestEncoder()

    sheet_a = project.add_sheet("Filings")
    col_a = project.add_column(sheet_a, "summary")
    rows_a = project.add_rows(
        sheet_a,
        [
            {"summary": "procurement contract invoice"},
            {"summary": "wildlife bird survey"},
        ],
        {"summary": col_a},
    )

    sheet_b = project.add_sheet("Attachments")
    col_b_text = project.add_column(sheet_b, "note")
    col_b_img = project.add_column(sheet_b, "scan", type="image")
    red_hash = project.add_blob(_png_bytes("red"), mime="image/png", filename="red.png")
    green_hash = project.add_blob(
        _png_bytes("green"), mime="image/png", filename="green.png"
    )
    rows_b = project.add_rows(
        sheet_b,
        [
            {"note": "unrelated maritime manifest", "scan": {"blob": red_hash}},
            {"note": "forest report", "scan": {"blob": green_hash}},
        ],
        {"note": col_b_text, "scan": col_b_img},
    )

    _drain_local(project, encoder)

    sheet_spec = {
        "scope": {"kind": "sheet", "sheet_id": sheet_a},
        "similarity": {
            "kind": "local_retrieval",
            "query_text": "urgent contract",
            "min_score": 0.85,
        },
    }
    with open_selection(project, sheet_spec, embedding_encoder=encoder) as selected:
        hits = list(selected.hits())
        assert [(h["sheet_id"], h["row_id"]) for h in hits] == [(sheet_a, rows_a[0])]
        assert hits[0]["similarity_score"] >= 0.85

    calls_after_first_query = encoder.calls

    # Project scope spans both sheets and matches raw images directly without OCR.
    project_spec = {
        "scope": {"kind": "project"},
        "similarity": {
            "kind": "local_retrieval",
            "query_text": "urgent contract",
            "min_score": 0.85,
        },
    }
    with open_selection(project, project_spec, embedding_encoder=encoder) as selected:
        hits = list(selected.hits())
        assert {(h["sheet_id"], h["row_id"]) for h in hits} == {
            (sheet_a, rows_a[0]),
            (sheet_b, rows_b[0]),
        }
        assert all(h["similarity_score"] >= 0.85 for h in hits)

    # Re-evaluating the same query_text hits the representation cache (0 extra encoder calls).
    assert encoder.calls == calls_after_first_query


def test_local_retrieval_view_multimodal_source_anchor_image_and_pdf_page(
    make_project, monkeypatch
) -> None:
    async def fake_render(pdf_path, out_dir, **kwargs):
        del pdf_path
        pages = kwargs.get("pages") or [1, 2]
        out_pages: list[tuple[int, Path]] = []
        for page in pages:
            target = Path(out_dir) / f"page_{page}.png"
            target.write_bytes(_png_bytes("red" if page == 1 else "green"))
            out_pages.append((page, target))
        return type("Rendered", (), {"pages": out_pages, "page_count": 2})()

    monkeypatch.setattr(preparation_mod, "render_pdf_pages", fake_render)

    project = make_project("local-retrieval-anchor")
    encoder = _MultimodalTestEncoder()

    sheet_ref = project.add_sheet("References")
    col_ref = project.add_column(sheet_ref, "photo", type="image")
    ref_red_hash = project.add_blob(
        _png_bytes("red"), mime="image/png", filename="target.png"
    )
    rows_ref = project.add_rows(
        sheet_ref,
        [{"photo": {"blob": ref_red_hash}}],
        {"photo": col_ref},
    )

    sheet_docs = project.add_sheet("Incoming")
    col_pdf = project.add_column(sheet_docs, "doc", type="file")
    col_img = project.add_column(sheet_docs, "img", type="image")
    pdf_hash = project.add_blob(
        _two_page_pdf_bytes(), mime="application/pdf", filename="packet.pdf"
    )
    blue_hash = project.add_blob(
        _png_bytes("blue"), mime="image/png", filename="blue.png"
    )
    rows_docs = project.add_rows(
        sheet_docs,
        [
            {"doc": {"blob": pdf_hash}},
            {"img": {"blob": blue_hash}},
        ],
        {"doc": col_pdf, "img": col_img},
    )

    _drain_local(project, encoder)
    calls_before_query = encoder.calls

    # Image SourceAnchor queries across the project (finds PDF page 1 visually; excludes exact anchor).
    image_anchor_spec = {
        "scope": {"kind": "project"},
        "similarity": {
            "kind": "local_retrieval",
            "min_score": 0.9,
            "source_anchor": {
                "source": {
                    "source_id": f"blob:{ref_red_hash}",
                    "source_kind": "blob",
                    "version_kind": "blob_digest",
                    "version_value": ref_red_hash,
                    "content_hash": f"sha256:{ref_red_hash}",
                },
                "locator": {"kind": "image"},
                "exclude_exact": True,
            },
        },
    }
    with open_selection(
        project, image_anchor_spec, embedding_encoder=encoder
    ) as selected:
        hits = list(selected.hits())
        assert [(h["sheet_id"], h["row_id"]) for h in hits] == [
            (sheet_docs, rows_docs[0])
        ]
        assert hits[0]["similarity_score"] >= 0.99

    # Prepared SourceAnchor resolved directly from retrieval store -> 0 encoder calls!
    assert encoder.calls == calls_before_query

    # PDF page SourceAnchor queries across project -> finds the matching reference image!
    pdf_anchor_spec = {
        "scope": {"kind": "project"},
        "similarity": {
            "kind": "local_retrieval",
            "min_score": 0.9,
            "source_anchor": {
                "source": {
                    "source_id": f"blob:{pdf_hash}",
                    "source_kind": "blob",
                    "version_kind": "blob_digest",
                    "version_value": pdf_hash,
                    "content_hash": f"sha256:{pdf_hash}",
                },
                "locator": {
                    "kind": "pdf_page_image",
                    "pages": {"start": 1, "end": 1},
                },
                "exclude_exact": True,
            },
        },
    }
    with open_selection(
        project, pdf_anchor_spec, embedding_encoder=encoder
    ) as selected:
        hits = list(selected.hits())
        assert [(h["sheet_id"], h["row_id"]) for h in hits] == [
            (sheet_ref, rows_ref[0])
        ]

    # Combined query_text + source_anchor composes both vectors.
    combined_spec = {
        "scope": {"kind": "project"},
        "similarity": {
            "kind": "local_retrieval",
            "min_score": 0.85,
            "query_text": "urgent contract",
            "source_anchor": {
                "source": {
                    "source_id": f"blob:{ref_red_hash}",
                    "source_kind": "blob",
                    "version_kind": "blob_digest",
                    "version_value": ref_red_hash,
                    "content_hash": f"sha256:{ref_red_hash}",
                },
                "locator": {"kind": "image"},
                "exclude_exact": True,
            },
        },
    }
    with open_selection(project, combined_spec, embedding_encoder=encoder) as selected:
        hits = list(selected.hits())
        assert [(h["sheet_id"], h["row_id"]) for h in hits] == [
            (sheet_docs, rows_docs[0])
        ]


def test_local_retrieval_validation_and_threshold_pinned_alert_stability(
    make_project,
) -> None:
    project = make_project("local-retrieval-stability")
    encoder = _MultimodalTestEncoder()
    sheet = project.add_sheet("Feed")
    col = project.add_column(sheet, "title")
    rows = project.add_rows(
        sheet,
        [
            {"title": "contract notice"},
            {"title": "wildlife bulletin"},
        ],
        {"title": col},
    )
    _drain_local(project, encoder)

    # Missing min_score is rejected (no bare top-K-only local_retrieval view).
    with pytest.raises(ValueError, match="min_score"):
        normalize_selection(
            project,
            {
                "scope": {"kind": "sheet", "sheet_id": sheet},
                "similarity": {
                    "kind": "local_retrieval",
                    "query_text": "contract",
                },
                "limit": 5,
            },
        )

    # Invalid min_score bounds are rejected.
    for bad_score in (0.0, -0.5, 1.5, float("nan"), True):
        with pytest.raises(ValueError, match="min_score"):
            normalize_selection(
                project,
                {
                    "scope": {"kind": "sheet", "sheet_id": sheet},
                    "similarity": {
                        "kind": "local_retrieval",
                        "query_text": "contract",
                        "min_score": bad_score,
                    },
                },
            )

    # Missing both query_text and source_anchor is rejected.
    with pytest.raises(ValueError, match="query_text"):
        normalize_selection(
            project,
            {
                "scope": {"kind": "sheet", "sheet_id": sheet},
                "similarity": {
                    "kind": "local_retrieval",
                    "min_score": 0.7,
                },
            },
        )

    # Binding an Alert to a local_retrieval View with a top-K limit is forbidden.
    capped_view = project.add_view(
        "Capped local view",
        {
            "selection": {
                "scope": {"kind": "sheet", "sheet_id": sheet},
                "similarity": {
                    "kind": "local_retrieval",
                    "query_text": "urgent contract",
                    "min_score": 0.7,
                },
                "limit": 1,
            },
            "presentation": {},
        },
    )
    with pytest.raises(ValueError, match="top-K limit"):
        project.add_alert("Capped alert", view_id=capped_view)

    # Threshold-pinned View + Alert: adding higher-scoring rows never evicts existing matches.
    pinned_view = project.add_view(
        "Pinned contracts",
        {
            "selection": {
                "scope": {"kind": "sheet", "sheet_id": sheet},
                "similarity": {
                    "kind": "local_retrieval",
                    "query_text": "urgent contract",
                    "min_score": 0.85,
                },
            },
            "presentation": {},
        },
    )
    alert_id = project.add_alert(
        "Contract monitor",
        view_id=pinned_view,
        detection_policy={
            "kind": "conditions",
            "entered": True,
            "exited": True,
        },
    )

    # Cannot mutate an Alert-bound local_retrieval View to add a top-K limit later.
    with pytest.raises(ValueError, match="top-K limit"):
        project.update_view(
            pinned_view,
            spec={
                "selection": {
                    "scope": {"kind": "sheet", "sheet_id": sheet},
                    "similarity": {
                        "kind": "local_retrieval",
                        "query_text": "urgent contract",
                        "min_score": 0.85,
                    },
                    "limit": 2,
                },
                "presentation": {},
            },
        )

    baseline = check_alert(
        project, project.get_alert(alert_id), embedding_encoder=encoder
    )
    assert baseline["run"]["status"] == "ok"
    assert baseline["run"]["matched_rows"] == 1

    # Add 3 higher-scoring matching rows; original row must remain matched (0 evictions!).
    added = project.add_rows(
        sheet,
        [
            {"title": "urgent contract 1"},
            {"title": "urgent contract 2"},
            {"title": "urgent contract 3"},
        ],
        {"title": col},
    )
    _drain_local(project, encoder)

    second = check_alert(
        project, project.get_alert(alert_id), embedding_encoder=encoder
    )
    assert second["run"]["status"] == "ok"
    assert second["run"]["matched_rows"] == 4
    assert second["run"]["new_rows"] == 3
    events = project.alert_check_events(second["run_id"], limit=20)
    assert [e["event_kind"] for e in events].count("row_entered") == 3
    assert [e["event_kind"] for e in events].count("row_exited") == 0
    matched_ids = {hit["row_id"] for hit in second["hits"]}
    assert matched_ids == {rows[0], *added}


def test_local_retrieval_graceful_degradation_when_weights_or_index_unavailable(
    make_project,
) -> None:
    project = make_project("local-retrieval-unavailable")
    sheet = project.add_sheet("Records")
    col = project.add_column(sheet, "body")
    project.add_rows(sheet, [{"body": "contract filing"}], {"body": col})

    view_id = project.add_view(
        "Local contracts",
        {
            "selection": {
                "scope": {"kind": "project"},
                "similarity": {
                    "kind": "local_retrieval",
                    "query_text": "urgent contract",
                    "min_score": 0.8,
                },
            },
            "presentation": {},
        },
    )
    alert_id = project.add_alert(
        "Local contract alert",
        view_id=view_id,
        detection_policy={"kind": "conditions", "entered": True, "exited": True},
    )

    missing_encoder = _MultimodalTestEncoder(is_available=False)

    # 1. Before retrieval sidecar exists: typed retrieval_not_prepared (never 0 matches).
    with pytest.raises(SelectionUnavailable) as exc_info:
        with open_selection(
            project,
            {
                "scope": {"kind": "project"},
                "similarity": {
                    "kind": "local_retrieval",
                    "query_text": "urgent contract",
                    "min_score": 0.8,
                },
            },
            embedding_encoder=missing_encoder,
        ):
            pass
    assert exc_info.value.code == "retrieval_not_prepared"

    check_before = check_alert(
        project, project.get_alert(alert_id), embedding_encoder=missing_encoder
    )
    assert check_before["run"]["status"] == "error"
    assert check_before["run"]["error_code"] == "retrieval_not_prepared"
    assert project.get_alert(alert_id)["baseline_run_id"] is None

    # 2. Index runs while weights aren't installed -> marks preparation encoder_unavailable.
    process_index_batches(project, embedding_encoder=missing_encoder)
    with pytest.raises(SelectionUnavailable) as exc_unavail:
        with open_selection(
            project,
            {
                "scope": {"kind": "project"},
                "similarity": {
                    "kind": "local_retrieval",
                    "query_text": "urgent contract",
                    "min_score": 0.8,
                },
            },
            embedding_encoder=missing_encoder,
        ):
            pass
    assert exc_unavail.value.code == "local_embedding_model_unavailable"

    check_unavail = check_alert(
        project, project.get_alert(alert_id), embedding_encoder=missing_encoder
    )
    assert check_unavail["run"]["status"] == "error"
    assert check_unavail["run"]["error_code"] == "local_embedding_model_unavailable"

    # 3. Install weights -> requeue unavailable & prepare -> baseline succeeds.
    with RetrievalStore(project).open() as store:
        store.requeue_unavailable()
    working_encoder = _MultimodalTestEncoder(is_available=True)
    _drain_local(project, working_encoder)

    baseline = check_alert(
        project, project.get_alert(alert_id), embedding_encoder=working_encoder
    )
    assert baseline["run"]["status"] == "ok"
    assert baseline["run"]["matched_rows"] == 1

    # 4. New row arrives and isn't prepared yet -> selection_incomplete, never clears Alert!
    project.add_rows(sheet, [{"body": "another contract"}], {"body": col})
    incomplete = check_alert(
        project, project.get_alert(alert_id), embedding_encoder=working_encoder
    )
    assert incomplete["run"]["status"] == "error"
    assert incomplete["run"]["error_code"] == "selection_incomplete"
    assert project.get_alert(alert_id)["baseline_run_id"] == baseline["run_id"]
    assert not project.alert_check_events(incomplete["run_id"])


def test_feed_poll_triggers_local_retrieval_alert_with_zero_paid_calls(
    tmp_path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pid = "feed-local-alert"
    project = Project.create(workspace / f"{pid}.frisket", name="Feed Local Alert")
    with write_transaction(project.db):
        feed_id = FeedStore(project).add_feed(
            "Filings",
            kind="rss",
            url="https://feeds.example/filings.xml",
            config={"merge_strategy": "append-new"},
            schedule="@hourly",
        )

    view_id = project.add_view(
        "Procurement matches",
        {
            "selection": {
                "scope": {"kind": "project"},
                "similarity": {
                    "kind": "local_retrieval",
                    "query_text": "urgent contract",
                    "min_score": 0.85,
                },
            },
            "presentation": {},
        },
    )
    alert_id = project.add_alert(
        "Procurement alert",
        view_id=view_id,
        detection_policy={"kind": "new_matches"},
        schedule="@hourly",
    )
    project.close()

    queue = SqliteJobQueue(workspace / "jobs.sqlite")
    encoder = _MultimodalTestEncoder(is_available=True)
    feeds = [
        _rss_feed(
            [
                {
                    "guid": "g1",
                    "title": "Initial procurement contract",
                    "link": "https://example/1",
                    "description": "contract invoice filed",
                }
            ]
        ),
        _rss_feed(
            [
                {
                    "guid": "g1",
                    "title": "Initial procurement contract",
                    "link": "https://example/1",
                    "description": "contract invoice filed",
                },
                {
                    "guid": "g2",
                    "title": "Wildlife census",
                    "link": "https://example/2",
                    "description": "forest bird migration",
                },
                {
                    "guid": "g3",
                    "title": "Urgent contract award",
                    "link": "https://example/3",
                    "description": "urgent procurement contract",
                },
            ]
        ),
    ]
    poll_index = {"n": 0}

    def fetch(_url: str) -> str:
        body = feeds[poll_index["n"]]
        poll_index["n"] += 1
        return body

    worker = Worker(queue)
    register_feed_poll_handler(
        worker.registry,
        workspace_root=workspace,
        queue=queue,
        fetch=fetch,
    )
    register_alert_evaluate_handler(
        worker.registry,
        workspace_root=workspace,
        queue=queue,
        embedding_encoder=encoder,
    )

    # Poll #1 -> feed.poll enqueues alert.evaluate -> prepares locally & sets silent baseline.
    queue.enqueue(
        FEED_POLL_KIND,
        {
            "project_id": pid,
            "feed_id": feed_id,
            "poll_id": "p1",
            "workspace_root": str(workspace),
        },
    )
    while worker.run_once():
        pass

    check_proj = Project(workspace / f"{pid}.frisket")
    try:
        first_run = check_proj.alert_latest_run(alert_id)
        assert first_run is not None
        assert first_run["status"] == "ok"
        assert first_run["matched_rows"] == 1
        assert first_run["new_rows"] == 0
    finally:
        check_proj.close()

    # Poll #2 -> new matching item + non-matching item -> Alert fires cleanly!
    queue.enqueue(
        FEED_POLL_KIND,
        {
            "project_id": pid,
            "feed_id": feed_id,
            "poll_id": "p2",
            "workspace_root": str(workspace),
        },
    )
    while worker.run_once():
        pass

    check_proj = Project(workspace / f"{pid}.frisket")
    try:
        second_run = check_proj.alert_latest_run(alert_id)
        assert second_run is not None
        assert second_run["status"] == "ok"
        assert second_run["matched_rows"] == 2
        assert second_run["new_rows"] == 1
        events = check_proj.alert_check_events(int(second_run["id"]), limit=10)
        assert [e["event_kind"] for e in events] == ["row_entered"]
        notifications = check_proj.db.execute(
            "SELECT * FROM notification_items WHERE source_kind='alert'"
        ).fetchall()
        assert len(notifications) == 1
    finally:
        check_proj.close()


def test_alert_evaluate_worker_unavailable_encoder_does_not_hot_loop(
    tmp_path,
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    pid = "worker-unavailable"
    project = Project.create(workspace / f"{pid}.frisket", name="Worker Unavailable")
    sheet = project.add_sheet("Docs")
    col = project.add_column(sheet, "body")
    project.add_rows(sheet, [{"body": "urgent contract"}], {"body": col})
    view_id = project.add_view(
        "Contracts",
        {
            "selection": {
                "scope": {"kind": "project"},
                "similarity": {
                    "kind": "local_retrieval",
                    "query_text": "urgent contract",
                    "min_score": 0.8,
                },
            },
            "presentation": {},
        },
    )
    alert_id = project.add_alert(
        "Contract alert",
        view_id=view_id,
        detection_policy={"kind": "new_matches"},
        schedule="@hourly",
    )
    missing_encoder = _MultimodalTestEncoder(is_available=False)
    process_index_batches(project, embedding_encoder=missing_encoder)
    project.close()

    queue = SqliteJobQueue(workspace / "jobs.sqlite")
    worker = Worker(queue)
    register_alert_evaluate_handler(
        worker.registry,
        workspace_root=workspace,
        queue=queue,
        embedding_encoder=missing_encoder,
    )
    queue.enqueue(
        ALERT_EVALUATE_KIND,
        {
            "project_id": pid,
            "alert_id": alert_id,
            "workspace_root": str(workspace),
            "trigger_ref": {"trigger_kind": "schedule", "schedule_tick": 1},
            "dedupe_key": f"alert:{alert_id}:eval:sched-1",
        },
    )
    ran = 0
    while worker.run_once():
        ran += 1
        assert ran <= 3, "alert.evaluate hot-looped when encoder was unavailable"
    assert ran == 1

    check_proj = Project(workspace / f"{pid}.frisket")
    try:
        latest = check_proj.alert_latest_run(alert_id)
        assert latest is not None
        assert latest["status"] == "error"
        assert latest["error_code"] == "local_embedding_model_unavailable"
    finally:
        check_proj.close()


def test_http_views_and_alerts_local_retrieval_end_to_end(tmp_path) -> None:
    encoder = _MultimodalTestEncoder(is_available=True)
    app = create_app(
        tmp_path / "ws",
        router=ModelRouter(model_call_policy=LOCAL_PURCHASE_POLICY, keys={}),
        embedding_runtime=encoder,
    )
    with TestClient(app) as client:
        pid = client.post("/api/projects", json={"name": "http-local"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Filings")
        col = project.add_column(sheet, "text")
        rows = project.add_rows(
            sheet,
            [
                {"text": "urgent contract filing"},
                {"text": "wildlife bird report"},
            ],
            {"text": col},
        )
        _drain_local(project, encoder)

        created = client.post(
            f"/api/projects/{pid}/views",
            json={
                "name": "Local contracts",
                "selection": {
                    "scope": {"kind": "project"},
                    "similarity": {
                        "kind": "local_retrieval",
                        "query_text": "urgent contract",
                        "min_score": 0.85,
                    },
                },
            },
        )
        assert created.status_code == 200, created.text
        view_id = created.json()["id"]

        resolved = client.get(f"/api/projects/{pid}/views/{view_id}/resolve")
        assert resolved.status_code == 200, resolved.text
        body = resolved.json()
        assert body["total"] == 1
        assert [(r["sheet_id"], r["row_id"]) for r in body["refs"]] == [
            (sheet, rows[0])
        ]
        assert body["refs"][0]["similarity_score"] >= 0.85

        alert = client.post(
            f"/api/projects/{pid}/alerts",
            json={
                "name": "Local contract watch",
                "view_id": view_id,
                "detection_policy": {"kind": "new_matches"},
            },
        )
        assert alert.status_code == 200, alert.text
        alert_id = alert.json()["id"]

        checked = client.post(f"/api/projects/{pid}/alerts/{alert_id}/check")
        assert checked.status_code == 200, checked.text
        assert checked.json()["run"]["status"] == "ok"
        assert checked.json()["run"]["matched_rows"] == 1


def test_cross_sheet_blob_dedup_pdf_and_authored_page(
    make_project, monkeypatch
) -> None:
    render_calls = 0

    async def counting_render(pdf_path, out_dir, **kwargs):
        nonlocal render_calls
        del pdf_path
        render_calls += 1
        pages = kwargs.get("pages") or [1, 2]
        out_pages: list[tuple[int, Path]] = []
        for page in pages:
            target = Path(out_dir) / f"page_{page}.png"
            target.write_bytes(_png_bytes("red" if page == 1 else "green"))
            out_pages.append((page, target))
        return type("Rendered", (), {"pages": out_pages, "page_count": 2})()

    monkeypatch.setattr(preparation_mod, "render_pdf_pages", counting_render)

    project = make_project("cross-sheet-pdf-dedup")
    encoder = _MultimodalTestEncoder()
    pdf_hash = project.add_blob(
        _two_page_pdf_bytes(), mime="application/pdf", filename="shared.pdf"
    )

    sheet_a = project.add_sheet("Sheet A")
    col_a = project.add_column(sheet_a, "file", type="file")
    project.add_rows(sheet_a, [{"file": {"blob": pdf_hash}}], {"file": col_a})

    _drain_dirty(project)
    step_a = process_blob_image_preparation_batch(
        project, channel=PDF_PAGE_CHANNEL, encoder=encoder
    )
    assert step_a.complete and step_a.processed_parts == 2
    assert render_calls == 1
    assert encoder.calls == 1

    # Add exact same PDF blob_hash to Sheet B -> 0 duplicate renders, 0 duplicate encodings!
    sheet_b = project.add_sheet("Sheet B")
    col_b = project.add_column(sheet_b, "file", type="file")
    rows_b = project.add_rows(sheet_b, [{"file": {"blob": pdf_hash}}], {"file": col_b})
    _drain_dirty(project)
    assert preparation_work_marker(project.path) is None
    step_b = process_blob_image_preparation_batch(
        project, channel=PDF_PAGE_CHANNEL, encoder=encoder
    )
    assert step_b.complete and step_b.processed_parts == 0
    assert render_calls == 1
    assert encoder.calls == 1

    # Sheet B is immediately searchable via its own cell_source_binding_id!
    space = EmbeddingGemma2Space()
    query_vec = _unit_axis(space.dimensions, {0: 1.0})
    hits_b = list(
        RetrievalQueryService(project).iter_row_matches(
            SearchRequest(
                scope=RetrievalScope((SheetScope(sheet_b),)),
                query=WeightedSemanticQuery(
                    terms=(WeightedTerm("red"),), minimum_similarity=0.9
                ),
                mode="semantic",
                group_by="row",
                limit=10,
                space_id=space.identity,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=(query_vec,)),
        )
    )
    assert [(h["sheet_id"], h["row_id"]) for h in hits_b] == [(sheet_b, rows_b[0])]

    # Authored single-page reference (page 2) in Sheet C reuses whole-PDF page 2 vector with 0 renders/encodes!
    sheet_c = project.add_sheet("Sheet C Pages")
    col_c = project.add_column(sheet_c, "page", type="file")
    rows_c = project.add_rows(
        sheet_c, [{"page": {"blob": pdf_hash, "page": 2}}], {"page": col_c}
    )
    _drain_dirty(project)
    step_c = process_blob_image_preparation_batch(
        project, channel=PDF_PAGE_CHANNEL, encoder=encoder
    )
    assert step_c.complete and step_c.processed_parts == 1
    assert render_calls == 1
    assert encoder.calls == 1

    # Sheet C's authored page 2 matches green (axis 1), not red (axis 0)!
    green_vec = _unit_axis(space.dimensions, {1: 1.0})
    hits_c = list(
        RetrievalQueryService(project).iter_row_matches(
            SearchRequest(
                scope=RetrievalScope((SheetScope(sheet_c),)),
                query=WeightedSemanticQuery(
                    terms=(WeightedTerm("green"),), minimum_similarity=0.9
                ),
                mode="semantic",
                group_by="row",
                limit=10,
                space_id=space.identity,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=(green_vec,)),
        )
    )
    assert [(h["sheet_id"], h["row_id"]) for h in hits_c] == [(sheet_c, rows_c[0])]


def test_cross_sheet_blob_dedup_video_zero_duplicate_decode_or_encode(
    make_project, monkeypatch
) -> None:
    decode_calls = 0

    async def fake_produce_media(source, recipe, **kwargs):
        nonlocal decode_calls
        del source, recipe, kwargs
        decode_calls += 1
        frame = ImageEncodingInput(_png_bytes("red"))
        clip = LocatedVideoClip(
            start_seconds=0.0,
            end_seconds=4.0,
            frames=(
                LocatedFrame(
                    PresentationPoint(pts=0, time_base_num=1, time_base_den=1), frame
                ),
            ),
            input=VideoClipEncodingInput((frame,)),
        )
        return ProducedMedia(
            items=(clip,),
            coverage=MediaCoverage(
                source_start_seconds=0.0,
                source_end_seconds=4.0,
                indexed_spans=((0.0, 4.0),),
                complete=True,
            ),
        )

    monkeypatch.setattr(preparation_mod, "produce_media", fake_produce_media)

    project = make_project("cross-sheet-video-dedup")
    encoder = _MultimodalTestEncoder()
    video_hash = project.add_blob(
        b"fake-mp4-bytes-for-dedup-test", mime="video/mp4", filename="clip.mp4"
    )

    sheet_a = project.add_sheet("Raw Videos")
    col_a = project.add_column(sheet_a, "video", type="video")
    project.add_rows(sheet_a, [{"video": {"blob": video_hash}}], {"video": col_a})

    _drain_dirty(project)
    step_a = process_timed_media_preparation_batch(
        project, channel=VIDEO_CHANNEL, encoder=encoder
    )
    assert step_a.complete and step_a.processed_parts == 1
    assert decode_calls == 1
    assert encoder.calls == 1

    # Same video blob in Sheet B -> 0 duplicate ffmpeg decodes, 0 duplicate encodings!
    sheet_b = project.add_sheet("Derived Videos")
    col_b = project.add_column(sheet_b, "video", type="video")
    rows_b = project.add_rows(
        sheet_b, [{"video": {"blob": video_hash}}], {"video": col_b}
    )
    _drain_dirty(project)
    assert preparation_work_marker(project.path) is None
    step_b = process_timed_media_preparation_batch(
        project, channel=VIDEO_CHANNEL, encoder=encoder
    )
    assert step_b.complete and step_b.processed_parts == 0
    assert decode_calls == 1
    assert encoder.calls == 1

    # Sheet B is immediately searchable!
    space = EmbeddingGemma2Space()
    red_vec = _unit_axis(space.dimensions, {0: 1.0})
    hits_b = list(
        RetrievalQueryService(project).iter_row_matches(
            SearchRequest(
                scope=RetrievalScope((SheetScope(sheet_b),)),
                query=WeightedSemanticQuery(
                    terms=(WeightedTerm("red"),), minimum_similarity=0.9
                ),
                mode="semantic",
                group_by="row",
                limit=10,
                space_id=space.identity,
            ),
            semantic_vectors=SemanticQueryVectors(term_vectors=(red_vec,)),
        )
    )
    assert [(h["sheet_id"], h["row_id"]) for h in hits_b] == [(sheet_b, rows_b[0])]
