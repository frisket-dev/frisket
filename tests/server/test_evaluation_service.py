"""Expected answers remain live ordinary cells; evaluation preserves provenance."""

import csv
import io
from contextlib import closing
from types import SimpleNamespace

import pytest

from frisket.engine.store import Project
from frisket.server.route_errors import RouteError
from frisket.server.services.evaluation import EvaluationService
from frisket.server.services.evaluation_values import source_fingerprints


@pytest.fixture
def env(tmp_path, monkeypatch):
    with closing(Project.create(tmp_path / "p")) as project:
        sheet = project.add_sheet("Cases")
        cols = {
            name: project.add_column(sheet, name) for name in ("id", "source", "answer")
        }
        rows = project.add_rows(
            sheet,
            [
                {"id": "a", "source": "document one", "answer": "YES"},
                {"id": "b", "source": "document two", "answer": "YES"},
            ],
            cols,
        )
        expected = project.add_sheet("Known")
        ec = {name: project.add_column(expected, name) for name in ("id", "answer")}
        erows = project.add_rows(
            expected, [{"id": "a", "answer": "yes"}, {"id": "b", "answer": "yes"}], ec
        )
        workspace = SimpleNamespace(get=lambda _pid: project)
        previews = {}
        service = EvaluationService(
            workspace, preview_reader=lambda _pid, key: previews[key]
        )
        context = {
            "sheet": {"id": sheet, "name": "Cases"},
            "draft": {
                "action_id": "extract.llm",
                "scope": {"kind": "sheet_rows", "sheet_id": sheet},
                "params": {},
                "output_names": {"answer": "answer"},
            },
            "source_columns": [{"column_id": cols["source"], "name": "source"}],
            "output_columns": [
                {
                    "logical_key": "answer",
                    "column_id": cols["answer"],
                    "current_name": "answer",
                }
            ],
        }
        monkeypatch.setattr(service, "_context", lambda _pid, _basis, _rows: context)
        body = {
            "target_sheet_id": sheet,
            "expected_sheet_id": expected,
            "action_id": "extract.llm",
            "keys": [{"expected_column_id": ec["id"], "target_column_id": cols["id"]}],
            "fields": [
                {
                    "expected_column_id": ec["answer"],
                    "output_column_id": cols["answer"],
                    "logical_key": "answer",
                    "rule": "exact",
                    "hidden": False,
                    "source": "import",
                }
            ],
            "hidden": False,
        }
        basis = {"kind": "draft", "draft": context["draft"]}
        request = {
            "basis": basis,
            "row_ids": rows,
            "variants": [
                {"key": "A", "label": "Current", "source": {"kind": "current"}}
            ],
            "link_id": None,
        }
        yield SimpleNamespace(**locals())


def join(e):
    result = e.service.save_link("p", e.body)
    e.request["link_id"] = result["id"]
    return result


def test_live_projection_and_rule_change_without_rerun(env):
    e = env
    link = join(e)
    before = e.service.score("p", e.request)
    assert before["variants"][0]["scores"]["answer"]["accuracy"] == 0
    e.service.set_rule("p", e.ec["answer"], "ignore_case")
    assert (
        e.service.score("p", e.request)["variants"][0]["scores"]["answer"]["accuracy"]
        == 1
    )
    e.service.expected_answer(
        "p",
        {
            "basis": e.basis,
            "link_id": link["id"],
            "row_id": e.rows[0],
            "logical_key": "answer",
            "value": "changed",
            "rule": "exact",
        },
    )
    assert e.project.get_values(e.expected, e.ec["answer"])[e.erows[0]] == "changed"
    projection = e.service.projection("p", e.sheet, e.rows)
    cell = projection["rows"][str(e.rows[0])][projection["columns"][0]["id"]]
    assert cell["value"] == "changed"
    assert cell["source_row_id"] == e.erows[0]


def test_duplicate_expected_keys_block_ambiguous_truth(env):
    e = env
    e.project.add_rows(e.expected, [{"id": "a", "answer": "different"}], e.ec)
    preview = e.service.preview_link("p", e.body)
    assert preview["ambiguous_target"] == 1
    with pytest.raises(RouteError, match="ambiguous"):
        e.service.save_link("p", e.body)


def test_manual_score_reuses_only_same_row_unchanged_inputs(env):
    e = env
    join(e)
    e.service.set_rule("p", e.ec["answer"], "hand")
    score = e.service.judgment(
        "p",
        {
            "comparison": e.request,
            "variant_key": "A",
            "row_id": e.rows[0],
            "logical_key": "answer",
            "verdict": "match",
        },
    )
    verdicts = score["variants"][0]["verdicts"]
    assert verdicts[str(e.rows[0])]["answer"] == "match"
    assert verdicts[str(e.rows[1])]["answer"] == "unscored"
    # Ordinary source-cell edits invalidate hand judgments, output reruns do not.
    e.service._edit_cell("p", e.project, e.rows[0], e.cols["source"], "revised source")
    assert (
        e.service.score("p", e.request)["variants"][0]["verdicts"][str(e.rows[0])][
            "answer"
        ]
        == "unscored"
    )


def preview(e, key, rows):
    e.previews[key] = {
        "sheet_id": e.sheet,
        "action_id": "extract.llm",
        "output_columns": e.context["output_columns"],
        "rows": rows,
        "source_fingerprints": source_fingerprints(
            e.project, e.sheet, [e.cols["source"]], e.rows
        ),
        "settings": {"model": key},
    }


def test_not_run_is_excluded_failure_is_miss_no_unequal_coverage_winner(env):
    e = env
    join(e)
    preview(
        e, "partial", {str(e.rows[0]): {"answer": {"state": "value", "value": "yes"}}}
    )
    preview(
        e,
        "failed",
        {
            str(e.rows[0]): {"answer": {"state": "error", "error": "timeout"}},
            str(e.rows[1]): {"answer": {"state": "value", "value": "yes"}},
        },
    )
    e.request["variants"] = [
        {"key": key, "label": key, "source": {"kind": "preview", "preview_ids": [key]}}
        for key in ("partial", "failed")
    ]
    result = e.service.score("p", e.request)
    first, second = result["variants"]
    assert first["scores"]["answer"]["accuracy"] == 1
    assert first["scores"]["answer"]["coverage"] == 0.5
    assert second["scores"]["answer"]["accuracy"] == 0.5
    assert second["scores"]["answer"]["failed"] == 1
    assert result["best"]["answer"] == []


def test_shelf_copies_full_values_survives_preview_discard_and_exports_csv(env):
    e = env
    join(e)
    long_value = "paragraph,\n" * 10_000
    preview(
        e,
        "full",
        {
            str(e.rows[0]): {"answer": {"state": "value", "value": long_value}},
            str(e.rows[1]): {"answer": {"state": "error", "error": "failed"}},
        },
    )
    e.request["variants"] = [
        {
            "key": "B",
            "label": "Before",
            "source": {"kind": "preview", "preview_ids": ["full"]},
        }
    ]
    item = e.service.shelve("p", {"comparison": e.request, "variant_keys": ["B"]})[
        "items"
    ][0]
    e.previews.clear()
    e.request["variants"][0]["source"] = {"kind": "saved", "saved_id": item["id"]}
    saved_cell = e.service.saved("p", item["id"])["rows"][str(e.rows[0])]["answer"]
    assert saved_cell["value"] is None
    assert saved_cell["bounded_value"]["total_characters"] == len(long_value)
    assert (
        e.service.cell(
            "p",
            {
                "comparison": e.request,
                "variant_key": "B",
                "row_id": e.rows[0],
                "logical_key": "answer",
            },
        )["value"]
        == long_value
    )
    csv.field_size_limit(1_000_000)
    records = list(csv.DictReader(io.StringIO(e.service.download_csv("p", e.request))))
    assert records[0]["answer_B"] == long_value
    assert records[1]["answer_B"] == "#FAILED"
    assert records[1]["answer_B_match"] == "no"


def test_disagreement_answer_creates_visible_ordinary_sheet_and_row_link(env):
    e = env
    link = e.service.expected_answer(
        "p",
        {
            "basis": e.basis,
            "link_id": None,
            "row_id": e.rows[0],
            "logical_key": "answer",
            "value": "yes",
            "rule": "exact",
        },
    )
    assert link["expected_sheet_name"] == "Cases · expected"
    assert any(s["id"] == link["expected_sheet_id"] for s in e.project.sheets())
    projected = e.service.projection("p", e.sheet, e.rows)
    assert (
        projected["rows"][str(e.rows[0])][projected["columns"][0]["id"]]["value"]
        == "yes"
    )
    assert not projected["rows"][str(e.rows[1])][projected["columns"][0]["id"]][
        "checked"
    ]


def test_preview_source_changes_cannot_receive_new_hand_judgment(env):
    e = env
    join(e)
    e.service.set_rule("p", e.ec["answer"], "hand")
    preview(e, "old", {str(e.rows[0]): {"answer": {"state": "value", "value": "yes"}}})
    e.service._edit_cell("p", e.project, e.rows[0], e.cols["source"], "changed")
    e.request["variants"] = [
        {
            "key": "B",
            "label": "Old",
            "source": {"kind": "preview", "preview_ids": ["old"]},
        }
    ]
    with pytest.raises(RouteError, match="source"):
        e.service.judgment(
            "p",
            {
                "comparison": e.request,
                "variant_key": "B",
                "row_id": e.rows[0],
                "logical_key": "answer",
                "verdict": "match",
            },
        )


def test_public_routes_accept_key_preview_before_field_mapping(env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from frisket.server.route_errors import register_route_error_handler
    from frisket.server.routes.project_evaluation import (
        register_project_evaluation_routes,
    )

    app = FastAPI()
    register_route_error_handler(app)
    register_project_evaluation_routes(app, service=env.service)
    with TestClient(app) as client:
        root = "/api/projects/p/evaluation"
        preview_body = {**env.body, "fields": []}
        preview_result = client.post(root + "/links/preview", json=preview_body)
        assert preview_result.status_code == 200, preview_result.text
        assert preview_result.json()["matched"] == 2
        assert client.post(root + "/links", json=preview_body).status_code == 422
        link = client.post(root + "/links", json=env.body)
        assert link.status_code == 200, link.text
        env.request["link_id"] = link.json()["id"]
        scope = client.post(
            root + "/rows", json={"link_id": link.json()["id"], "limit": 1}
        )
        assert scope.status_code == 200, scope.text
        assert scope.json() == {"row_ids": [env.rows[0]], "total": 2}
        assert (
            client.post(
                root + "/rows", json={"link_id": link.json()["id"], "limit": 501}
            ).status_code
            == 422
        )
        score = client.post(root + "/score", json=env.request)
        assert score.status_code == 200, score.text
        assert score.json()["variants"][0]["scores"]["answer"]["misses"] == 2
        download = client.post(root + "/download.csv", json=env.request)
        assert download.headers["content-type"].startswith("text/csv")
        assert len(list(csv.reader(io.StringIO(download.text)))) == 3
        assert (
            client.get(
                root + "/links", params={"sheet_id": env.sheet, "unexpected": "x"}
            ).status_code
            == 422
        )


def test_review_download_full_values_and_original_correctness(env):
    e = env
    op = e.project.append_op("map", {"action_id": "extract.llm"})
    run = e.project.db.execute(
        "INSERT INTO runs(op_id,sheet_id,action_kind,status) VALUES (?,?,?,'completed')",
        (op, e.sheet, "extract.llm"),
    ).lastrowid
    e.project.db.execute(
        "INSERT INTO run_review_fields(run_id,column_id,column_name,column_type,column_position,is_primary) VALUES (?,?,?,'text',1,1)",
        (run, e.cols["answer"], "answer"),
    )
    long_value = "long,\n" * 20_000
    for row_id, value, decision in zip(
        e.rows, (long_value, "wrong"), ("accept", "edit"), strict=True
    ):
        e.project.db.execute(
            "INSERT INTO results(run_id,row_id,column_id,value_kind,value,review_decision) VALUES (?,?,?,'text',?,?)",
            (run, row_id, e.cols["answer"], value, decision),
        )
    e.project.db.commit()
    csv.field_size_limit(1_000_000)
    records = list(csv.DictReader(io.StringIO(e.service.review_csv("p", run))))
    assert records[0]["answer"] == long_value
    assert records[0]["answer_reviewed"] == "yes"
    assert records[1]["answer_reviewed"] == "no"


def test_preview_chunks_reject_overlap_and_changed_schema(env):
    e = env
    join(e)
    rows = {str(e.rows[0]): {"answer": {"state": "value", "value": "yes"}}}
    preview(e, "one", rows)
    preview(e, "two", rows)
    e.request["variants"] = [
        {
            "key": "B",
            "label": "B",
            "source": {"kind": "preview", "preview_ids": ["one", "two"]},
        }
    ]
    with pytest.raises(RouteError, match="repeat"):
        e.service.score("p", e.request)
    e.request["variants"][0]["source"]["preview_ids"] = ["one"]
    e.previews["one"]["action_id"] = "another-action"
    with pytest.raises(RouteError, match="schema"):
        e.service.score("p", e.request)


def test_captures_append_durable_chunks_then_shelve_and_discard_are_distinct(env):
    e = env
    join(e)
    preview(
        e, "first", {str(e.rows[0]): {"answer": {"state": "value", "value": "yes"}}}
    )
    preview(
        e, "second", {str(e.rows[1]): {"answer": {"state": "value", "value": "yes"}}}
    )
    # Chunk scope changes, actual model/instructions do not.
    e.previews["first"]["settings"] = {
        "model": "same",
        "scope": {"row_ids": [e.rows[0]]},
    }
    e.previews["second"]["settings"] = {
        "model": "same",
        "scope": {"row_ids": [e.rows[1]]},
    }
    e.request["variants"] = [
        {
            "key": "B",
            "label": "B",
            "source": {"kind": "preview", "preview_ids": ["first"]},
        }
    ]
    item = e.service.capture("p", {"comparison": e.request, "variant_keys": ["B"]})[
        "items"
    ][0]
    assert not item["kept"]
    assert not e.service.shelf("p", e.sheet, "extract.llm")["items"]
    e.request["variants"][0]["source"]["preview_ids"] = ["second"]
    appended = e.service.capture(
        "p", {"comparison": e.request, "variant_keys": ["B"], "target_id": item["id"]}
    )["items"][0]
    assert appended["id"] == item["id"]
    assert appended["row_count"] == 2
    e.previews.clear()
    e.request["variants"][0]["source"] = {"kind": "saved", "saved_id": item["id"]}
    assert (
        e.service.score("p", e.request)["variants"][0]["scores"]["answer"]["accuracy"]
        == 1
    )
    kept = e.service.shelve("p", {"comparison": e.request, "variant_keys": ["B"]})[
        "items"
    ][0]
    assert kept["id"] == item["id"] and kept["kept"]
    e.service.discard_capture("p", item["id"])
    assert e.service.saved("p", item["id"])["id"] == item["id"]
    assert len(e.service.shelf("p", e.sheet, "extract.llm")["items"]) == 1


def test_existing_rules_survive_join_edits_and_expected_answer_saves(env):
    e = env
    link = join(e)
    e.service.set_rule("p", e.ec["answer"], "ignore_case")
    e.service.save_link("p", e.body, link["id"])
    e.service.expected_answer(
        "p",
        {
            "basis": e.basis,
            "link_id": link["id"],
            "row_id": e.rows[0],
            "logical_key": "answer",
            "value": "",
            "rule": "exact",
        },
    )
    assert e.project.get_values(e.expected, e.ec["answer"])[e.erows[0]] is None
    assert (
        e.service.links("p", e.sheet)["links"][0]["fields"][0]["rule"] == "ignore_case"
    )


def test_score_display_is_bounded_but_full_cell_and_exports_are_exact(env):
    e = env
    join(e)
    text = "X" * (80 * 1024)
    preview(e, "long", {str(e.rows[0]): {"answer": {"state": "value", "value": text}}})
    e.request["variants"] = [
        {
            "key": "L",
            "label": "Long",
            "source": {"kind": "preview", "preview_ids": ["long"]},
        }
    ]
    result = e.service.score("p", e.request)
    cell = result["variants"][0]["rows"][str(e.rows[0])]["answer"]
    assert cell["value"] is None and len(cell["bounded_value"]["preview"]) <= 2048
    assert cell["value_hash"]
    assert result["variants"][0]["verdicts"][str(e.rows[0])]["answer"] == "miss"
    assert (
        e.service.cell(
            "p",
            {
                "comparison": e.request,
                "variant_key": "L",
                "row_id": e.rows[0],
                "logical_key": "answer",
            },
        )["value"]
        == text
    )
    assert text in e.service.download_csv("p", e.request)


def test_capture_cleanup_preserves_kept_and_recently_used_results(env):
    e = env
    captures = [
        e.service.capture("p", {"comparison": e.request, "variant_keys": ["A"]})[
            "items"
        ][0]["id"]
        for _ in range(3)
    ]
    kept, active, abandoned = captures
    e.request["variants"][0]["source"] = {"kind": "saved", "saved_id": kept}
    e.service.shelve("p", {"comparison": e.request, "variant_keys": ["A"]})
    e.project.db.execute(
        "UPDATE saved_compare_variants SET last_used_at=datetime('now','-8 days')"
    )
    e.project.db.commit()
    e.request["variants"][0]["source"] = {"kind": "saved", "saved_id": active}
    e.service.score("p", e.request)
    e.request["variants"][0]["source"] = {"kind": "current"}
    e.service.capture("p", {"comparison": e.request, "variant_keys": ["A"]})
    assert e.service.saved("p", kept)["kept"]
    assert e.service.saved("p", active)["id"] == active
    with pytest.raises(RouteError, match="not found"):
        e.service.saved("p", abandoned)


def test_not_run_csv_sentinel_distinguishes_empty_answer_and_unchecked(env):
    e = env
    join(e)
    preview(
        e, "partial", {str(e.rows[0]): {"answer": {"state": "null", "value": None}}}
    )
    e.request["variants"] = [
        {
            "key": "P",
            "label": "Partial",
            "source": {"kind": "preview", "preview_ids": ["partial"]},
        }
    ]
    rows = list(csv.DictReader(io.StringIO(e.service.download_csv("p", e.request))))
    assert rows[0]["answer_P"] == ""
    assert rows[0]["answer_P_match"] == "no"
    assert rows[1]["answer_P"] == "#NOT_RUN"
    assert rows[1]["answer_P_match"] == ""


def test_manual_empty_expectation_still_requires_judgment(env):
    e = env
    link = join(e)
    e.service.expected_answer(
        "p",
        {
            "basis": e.basis,
            "link_id": link["id"],
            "row_id": e.rows[0],
            "logical_key": "answer",
            "value": "[empty]",
            "rule": "exact",
        },
    )
    e.service.set_rule("p", e.ec["answer"], "hand")
    preview(e, "empty", {str(e.rows[0]): {"answer": {"state": "null", "value": None}}})
    e.request["variants"] = [
        {
            "key": "E",
            "label": "Empty",
            "source": {"kind": "preview", "preview_ids": ["empty"]},
        }
    ]
    assert (
        e.service.score("p", e.request)["variants"][0]["verdicts"][str(e.rows[0])][
            "answer"
        ]
        == "unscored"
    )
    result = e.service.judgment(
        "p",
        {
            "comparison": e.request,
            "variant_key": "E",
            "row_id": e.rows[0],
            "logical_key": "answer",
            "verdict": "miss",
        },
    )
    assert result["variants"][0]["verdicts"][str(e.rows[0])]["answer"] == "miss"


def test_equal_counts_with_disjoint_scored_rows_cannot_choose_winner(env):
    e = env
    join(e)
    preview(e, "a", {str(e.rows[0]): {"answer": {"state": "value", "value": "yes"}}})
    preview(e, "b", {str(e.rows[1]): {"answer": {"state": "value", "value": "yes"}}})
    e.request["variants"] = [
        {"key": key, "label": key, "source": {"kind": "preview", "preview_ids": [key]}}
        for key in ("a", "b")
    ]
    assert e.service.score("p", e.request)["best"]["answer"] == []


def test_join_ambiguity_created_after_save_refuses_reads(env):
    e = env
    join(e)
    e.project.add_rows(e.expected, [{"id": "a", "answer": "conflict"}], e.ec)
    with pytest.raises(RouteError, match="ambiguous"):
        e.service.score("p", e.request)
    with pytest.raises(RouteError, match="ambiguous"):
        e.service.projection("p", e.sheet, e.rows)


def test_expected_row_scope_counts_checked_matches_and_limits_only_returned_ids(env):
    e = env
    link = join(e)
    records = [
        {"id": str(index), "source": f"source {index}", "answer": "value"}
        for index in range(40)
    ]
    added = e.project.add_rows(e.sheet, records, e.cols)
    expectations = [
        {
            "id": str(index),
            "answer": None
            if index % 3 == 0
            else "[empty]"
            if index == 1
            else " "
            if index == 2
            else "large" * 20_000
            if index == 4
            else "value",
        }
        for index in range(40)
    ]
    e.project.add_rows(
        e.expected, [*expectations, {"id": "unmatched", "answer": "value"}], e.ec
    )
    checked = e.rows + [row_id for index, row_id in enumerate(added) if index % 3]
    limited = e.service.expected_rows("p", {"link_id": link["id"], "limit": 5})
    assert limited == {"row_ids": checked[:5], "total": len(checked)}
    assert len(checked) > 20
    assert e.service.expected_rows("p", {"link_id": link["id"]})["row_ids"] == checked
    e.project.add_rows(e.expected, [{"id": "a", "answer": "ambiguous"}], e.ec)
    with pytest.raises(RouteError, match="ambiguous"):
        e.service.expected_rows("p", {"link_id": link["id"]})


def test_expected_projection_reads_values_only_for_requested_matching_rows(
    env, monkeypatch
):
    from frisket.server.services import evaluation_links

    e = env
    join(e)
    original = evaluation_links.full_column
    reads = []

    def observed(project, sheet_id, column_id, row_ids):
        if column_id == e.ec["answer"]:
            reads.append(list(row_ids))
        return original(project, sheet_id, column_id, row_ids)

    monkeypatch.setattr(evaluation_links, "full_column", observed)
    result = e.service.projection("p", e.sheet, [e.rows[0]])
    assert result["rows"][str(e.rows[0])]
    assert reads == [[e.erows[0]]]


def test_expected_row_scope_accepts_any_mapped_field_and_resolves_keys_once(
    env, monkeypatch
):
    from frisket.server.services import evaluation_links

    e = env
    other_output = e.project.add_column(e.sheet, "other")
    other_expected = e.project.add_column(e.expected, "other")
    e.body["fields"].append(
        {
            "expected_column_id": other_expected,
            "output_column_id": other_output,
            "logical_key": "other",
            "rule": "exact",
            "hidden": False,
            "source": "import",
        }
    )
    link = join(e)
    e.service._edit_cell("p", e.project, e.erows[0], e.ec["answer"], None)
    e.service._edit_cell("p", e.project, e.erows[1], e.ec["answer"], "")
    e.service._edit_cell("p", e.project, e.erows[1], other_expected, "[empty]")
    calls = []
    original = evaluation_links.row_matches

    def observed(project, body):
        calls.append(body["target_sheet_id"])
        return original(project, body)

    monkeypatch.setattr(evaluation_links, "row_matches", observed)
    assert e.service.expected_rows("p", {"link_id": link["id"]}) == {
        "row_ids": [e.rows[1]],
        "total": 1,
    }
    assert calls == [e.sheet]


def test_hand_score_reports_stale_saved_source_without_blocking_current_answer(env):
    e = env
    join(e)
    e.service.set_rule("p", e.ec["answer"], "hand")
    preview(e, "old", {str(e.rows[0]): {"answer": {"state": "value", "value": "YES"}}})
    old = {
        "key": "B",
        "label": "Old",
        "source": {"kind": "preview", "preview_ids": ["old"]},
    }
    e.request["variants"] = [old]
    saved = e.service.capture("p", {"comparison": e.request, "variant_keys": ["B"]})[
        "items"
    ][0]
    old["source"] = {"kind": "saved", "saved_id": saved["id"]}
    e.request["variants"] = [
        {"key": "A", "label": "Current", "source": {"kind": "current"}},
        old,
    ]
    e.service._edit_cell(
        "p", e.project, e.rows[0], e.cols["source"], "updated document"
    )
    current, historical = e.service.score("p", e.request)["variants"]
    assert current["stale_source_row_ids"] == []
    assert historical["stale_source_row_ids"] == [e.rows[0]]
    result = e.service.judgment(
        "p",
        {
            "comparison": e.request,
            "variant_key": "A",
            "row_id": e.rows[0],
            "logical_key": "answer",
            "verdict": "match",
        },
    )
    assert result["variants"][0]["verdicts"][str(e.rows[0])]["answer"] == "match"
    assert result["variants"][1]["verdicts"][str(e.rows[0])]["answer"] == "unscored"
    with pytest.raises(RouteError, match="source"):
        e.service.judgment(
            "p",
            {
                "comparison": e.request,
                "variant_key": "B",
                "row_id": e.rows[0],
                "logical_key": "answer",
                "verdict": "match",
            },
        )


def test_hand_judgment_reuses_integer_and_integral_float_answer(env):
    e = env
    join(e)
    e.service.set_rule("p", e.ec["answer"], "hand")
    preview(
        e, "float", {str(e.rows[0]): {"answer": {"state": "value", "value": 925000.0}}}
    )
    e.request["variants"] = [
        {
            "key": "F",
            "label": "Float",
            "source": {"kind": "preview", "preview_ids": ["float"]},
        }
    ]
    e.service.judgment(
        "p",
        {
            "comparison": e.request,
            "variant_key": "F",
            "row_id": e.rows[0],
            "logical_key": "answer",
            "verdict": "match",
        },
    )
    preview(e, "int", {str(e.rows[0]): {"answer": {"state": "value", "value": 925000}}})
    e.request["variants"] = [
        {
            "key": "I",
            "label": "Integer",
            "source": {"kind": "preview", "preview_ids": ["int"]},
        }
    ]
    result = e.service.score("p", e.request)
    assert result["variants"][0]["verdicts"][str(e.rows[0])]["answer"] == "match"


def test_hand_value_hash_preserves_types_while_normalizing_integral_numbers():
    from frisket.server.services.evaluation_values import value_hash

    assert value_hash({"items": [925000.0, 0.0]}) == value_hash({"items": [925000, 0]})
    assert value_hash(False) != value_hash(0)
    assert value_hash("925000") != value_hash(925000)
