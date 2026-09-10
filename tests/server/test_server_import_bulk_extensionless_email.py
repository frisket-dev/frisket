from __future__ import annotations

import io
import zipfile
from email.message import EmailMessage
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def _project(client: TestClient) -> str:
    response = client.post("/api/projects", json={"name": "Extensionless email"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _plan(
    client: TestClient,
    project_id: str,
    uploads: list[tuple[str, bytes, str, str]],
    *,
    expand_archive: bool = False,
) -> dict:
    parts: list[tuple[str, tuple[None, str] | tuple[str, bytes, str]]] = []
    for filename, content, content_type, logical_path in uploads:
        parts.extend(
            [
                ("files", (filename, content, content_type)),
                ("logical_paths", (None, logical_path)),
            ]
        )
    parts.append(("expand_archive", (None, str(expand_archive).lower())))
    response = client.post(f"/api/projects/{project_id}/import/bulk/plan", files=parts)
    assert response.status_code == 200, response.text
    return response.json()


def _execute(client: TestClient, project_id: str, plan: dict) -> dict:
    response = client.post(
        f"/api/projects/{project_id}/import/bulk/{plan['plan_id']}/execute",
        json={"decisions": {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _sheet_rows(
    client: TestClient, project_id: str, sheet_id: int
) -> list[dict[str, object]]:
    response = client.get(f"/api/projects/{project_id}/sheets/{sheet_id}/data")
    assert response.status_code == 200, response.text
    data = response.json()
    names = {str(column["id"]): column["name"] for column in data["columns"]}
    return [
        {names[str(column_id)]: value for column_id, value in row["cells"].items()}
        for row in data["rows"]
    ]


def _message(subject: str) -> bytes:
    message = EmailMessage()
    message["From"] = "Reporter <reporter@example.test>"
    message["To"] = "Editor <editor@example.test>"
    message["Subject"] = subject
    message.set_content(f"Body for {subject}")
    return message.as_bytes()


def _mbox(*subjects: str) -> bytes:
    output = bytearray()
    for index, subject in enumerate(subjects):
        output.extend(
            f"From reporter@example.test Tue Sep  2 12:34:{index:02d} 2026\n".encode()
        )
        output.extend(_message(subject))
        output.extend(b"\n")
    return bytes(output)


def _zip(members: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for path, content in members.items():
            archive.writestr(path, content)
    return payload.getvalue()


def test_strong_extensionless_mbox_is_email_and_executes_stable_locations(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = _project(client)
    logical_path = "Takeout/Mail/archive"

    plan = _plan(
        client,
        project_id,
        [
            (
                "archive",
                _mbox("First message", "Second message"),
                "application/octet-stream",
                logical_path,
            )
        ],
    )

    assert plan["questions"] == []
    assert len(plan["proposed_outputs"]) == 1
    output = plan["proposed_outputs"][0]
    assert output["kind"] == "email"
    assert output["logical_paths"] == [logical_path]

    result = _execute(client, project_id, plan)
    assert result["failed"] == []
    rows = _sheet_rows(client, project_id, result["created"][0]["sheet_id"])
    assert [row["subject"] for row in rows] == ["First message", "Second message"]
    assert [row["source_file"] for row in rows] == [
        f"{logical_path}#1",
        f"{logical_path}#2",
    ]


def test_zip_maildir_cur_and_new_messages_group_as_ordered_email_rows(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = _project(client)
    paths = ["Maildir/cur/1710000001.M1:2,S", "Maildir/new/1710000002.M2"]
    archive = _zip(
        {
            paths[1]: _message("New message"),
            paths[0]: _message("Current message"),
        }
    )

    plan = _plan(
        client,
        project_id,
        [("maildir.zip", archive, "application/zip", "maildir.zip")],
        expand_archive=True,
    )

    assert plan["questions"] == []
    assert len(plan["proposed_outputs"]) == 1
    output = plan["proposed_outputs"][0]
    assert output["kind"] == "email"
    assert output["logical_paths"] == paths

    result = _execute(client, project_id, plan)
    assert result["failed"] == []
    rows = _sheet_rows(client, project_id, result["created"][0]["sheet_id"])
    assert [row["subject"] for row in rows] == ["Current message", "New message"]
    assert [row["source_file"] for row in rows] == paths


def test_ordinary_extensionless_files_remain_generic_files(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    project_id = _project(client)
    paths = ["folder/README", "folder/payload"]

    plan = _plan(
        client,
        project_id,
        [
            ("README", b"ordinary notes\n", "text/plain", paths[0]),
            ("payload", b"\x00\x01\x02not mail", "application/octet-stream", paths[1]),
        ],
    )

    assert plan["questions"] == []
    assert len(plan["proposed_outputs"]) == 1
    output = plan["proposed_outputs"][0]
    assert output["kind"] == "files_group"
    assert output["logical_paths"] == paths
