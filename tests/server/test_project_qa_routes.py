from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_shared_history_revision_and_project_boundary(tmp_path):
    with TestClient(create_app(tmp_path / "ws")) as client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        other = client.post("/api/projects", json={"name": "Other"}).json()["id"]
        path = f"/api/projects/{pid}/qa/threads"
        response = client.post(path, json={"scope": {"kind": "project"}})
        assert response.status_code == 200, response.text
        thread = response.json()
        detail = f"{path}/{thread['id']}"
        assert client.get(detail).json()["thread"] == thread
        assert (
            client.get(f"/api/projects/{other}/qa/threads/{thread['id']}").status_code
            == 404
        )
        assert (
            client.patch(
                detail, json={"expected_revision": 1, "title": "New title"}
            ).status_code
            == 200
        )
        assert (
            client.patch(
                detail, json={"expected_revision": 1, "title": "Stale"}
            ).status_code
            == 409
        )
        assert (
            client.get(detail + "/events", params={"after": 1, "before": 2}).status_code
            == 422
        )
        assert (
            client.get(detail + "/report").json()["markdown"].startswith("# New title")
        )
        assert client.get(detail + "/events?typo=1").status_code == 422
        assert client.delete(detail).status_code == 204
        assert client.get(path).json() == []


def test_missing_scope_is_rejected_before_admission(tmp_path):
    with TestClient(create_app(tmp_path / "ws")) as client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        path = f"/api/projects/{pid}/qa/threads"
        missing = {"kind": "sources", "sources": [{"kind": "sheet", "sheet_id": 999}]}
        assert client.post(path, json={"scope": missing}).status_code == 422
        thread = client.post(path, json={"scope": {"kind": "project"}}).json()
        detail = f"{path}/{thread['id']}"
        assert (
            client.patch(
                detail, json={"expected_revision": 1, "scope": missing}
            ).status_code
            == 422
        )
        assert (
            client.post(
                detail + "/turns",
                json={"request_id": "bad", "question": "Read", "scope": missing},
            ).status_code
            == 422
        )
        result = client.get(detail).json()
        assert result["active_turn"] is None
        assert result["history"]["events"] == []


def test_citation_resolves_saved_value_and_refuses_another_thread(tmp_path):
    from frisket.engine.store.project_qa import ProjectQAStore
    from frisket.server.services.project_qa_tools import ProjectQATools

    with TestClient(create_app(tmp_path / "ws")) as client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Evidence")
        column = project.add_column(sheet, "Name")
        [row] = project.add_rows(sheet, [{"Name": "Ada"}], {"Name": column})
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Question")
        other = store.create_thread(title="Other")
        turn = store.submit_turn(thread["id"], request_id="once", question="Who?")
        read = ProjectQATools(project, turn, store).read_rows(sheet)
        citation_id = read["rows"][0]["cells"][0]["citation_id"]
        store.finish_turn(turn["id"], status="completed")
        prefix = f"/api/projects/{pid}/qa/threads"
        path = f"{prefix}/{thread['id']}/citations/{citation_id}"
        result = client.get(path)
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "current"
        assert result.json()["target"] == {
            "kind": "cell",
            "sheet_id": sheet,
            "row_id": row,
            "column_id": column,
        }
        assert (
            client.get(f"{prefix}/{other['id']}/citations/{citation_id}").status_code
            == 404
        )
        project.apply_edits([{"row_id": row, "column_id": column, "value": "Grace"}])
        changed = client.get(path).json()
        assert changed["status"] == "changed"
        assert "Ada" in changed["excerpt"]
        project.delete_sheet(sheet)
        assert client.get(path).json()["status"] == "unavailable"


def test_query_and_prepared_evidence_citations_reopen_current_sources(tmp_path):
    from frisket.engine.store.evidence import (
        record_evidence_link,
        record_source_artifact,
        record_source_span,
    )
    from frisket.engine.store.project_qa import ProjectQAStore
    from frisket.server.services.project_qa_tools import ProjectQATools

    with TestClient(create_app(tmp_path / "ws")) as client:
        pid = client.post("/api/projects", json={"name": "Sources"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Evidence")
        column = project.add_column(sheet, "Text")
        rows = project.add_rows(
            sheet, [{"Text": "Ada"}, {"Text": "Grace"}], {"Text": column}
        )
        _, refs = project.get_values_with_refs(sheet, column, row_ids=rows)
        artifact = record_source_artifact(
            project,
            artifact_kind="text",
            media_type="text/plain",
            title="Original document",
        )
        span = record_source_span(
            project,
            artifact_id=artifact["id"],
            span_kind="text",
            quote="Ada signed the letter.",
        )
        link = record_evidence_link(
            project,
            subject_kind="cell_value",
            subject_ref=refs[rows[0]],
            spans=[{"span_id": span["id"]}],
            sheet_id=sheet,
            row_id=rows[0],
            column_id=column,
        )
        scope = {
            "kind": "sources",
            "sources": [{"kind": "rows", "sheet_id": sheet, "row_ids": [rows[0]]}],
        }
        store = ProjectQAStore(project)
        thread = store.create_thread(title="Evidence", scope=scope)
        turn = store.submit_turn(
            thread["id"], request_id="one", question="Who signed?", scope=scope
        )
        tools = ProjectQATools(project, turn, store)
        query = tools.query_rows(
            {
                "schema_version": "frisket.query.v1",
                "kind": "sheet.filter",
                "scope": {"kind": "sheet", "sheet_id": sheet},
                "filter": {},
            },
            limit=0,
        )
        read = tools.read_rows(sheet, [rows[0]], [column])
        passages = tools.open_source(read["rows"][0]["cells"][0]["citation_id"])[
            "passages"
        ]
        citation_id = next(
            p["citation_id"] for p in passages if p["kind"] == "prepared_evidence"
        )
        store.append_event(
            turn["id"],
            kind="answer",
            payload={
                "text": "Ada signed.",
                "citation_ids": [citation_id, query["citation_id"]],
            },
        )
        store.finish_turn(turn["id"], status="completed")
        prefix = f"/api/projects/{pid}/qa/threads/{thread['id']}"
        result = client.get(f"{prefix}/citations/{citation_id}")
        assert result.status_code == 200, result.text
        assert result.json()["status"] == "current"
        assert result.json()["target"] == {
            "kind": "evidence",
            "sheet_id": sheet,
            "row_id": rows[0],
            "column_id": column,
            "evidence_link_id": link["stable_id"],
            "artifact_id": artifact["stable_id"],
            "span_id": span["stable_id"],
        }
        result = client.get(f"{prefix}/citations/{query['citation_id']}").json()
        assert result["target"]["total"] == 1
        assert result["target"]["row_ids"] == [rows[0]]
        events = client.get(prefix + "/events").json()["events"]
        suggestion = next(e for e in events if e["kind"] == "result_suggestion")
        assert suggestion["citations"][0]["target"]["kind"] == "query"
        project.apply_edits(
            [{"row_id": rows[0], "column_id": column, "value": "Changed"}]
        )
        assert (
            client.get(f"{prefix}/citations/{citation_id}").json()["status"]
            == "changed"
        )
        project.delete_sheet(sheet)
        assert (
            client.get(f"{prefix}/citations/{query['citation_id']}").json()["status"]
            == "unavailable"
        )


def test_patch_null_scope_does_not_broaden_and_blank_title_is_friendly(tmp_path):
    with TestClient(create_app(tmp_path / "ws")) as client:
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        project = client.app.state.workspace.get(pid)
        sheet = project.add_sheet("Scope")
        scope = {"kind": "sources", "sources": [{"kind": "sheet", "sheet_id": sheet}]}
        path = f"/api/projects/{pid}/qa/threads"
        thread = client.post(path, json={"scope": scope}).json()
        path += f"/{thread['id']}"
        response = client.patch(path, json={"expected_revision": 1, "scope": None})
        assert response.status_code == 200, response.text
        assert response.json()["scope"] == scope
        assert (
            client.patch(
                path,
                json={"expected_revision": response.json()["revision"], "title": "   "},
            ).status_code
            == 422
        )


def test_project_deletion_refuses_active_ask(tmp_path):
    import asyncio

    with TestClient(create_app(tmp_path / "ws")) as client:

        async def runner(*args):
            await asyncio.Event().wait()

        client.app.state.project_qa_service._runner = runner
        pid = client.post("/api/projects", json={"name": "Ask"}).json()["id"]
        path = f"/api/projects/{pid}/qa/threads"
        thread = client.post(path, json={"scope": {"kind": "project"}}).json()
        response = client.post(
            f"{path}/{thread['id']}/turns",
            json={
                "request_id": "one",
                "question": "Read",
                "scope": {"kind": "project"},
            },
        )
        assert response.status_code == 200, response.text
        deletion = client.request(
            "DELETE", f"/api/projects/{pid}", json={"confirm_name": "Ask"}
        )
        assert deletion.status_code == 409, deletion.text
        assert "Ask" in deletion.text
