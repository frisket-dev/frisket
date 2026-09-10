from __future__ import annotations

import hashlib
import io
import zipfile
from email.message import EmailMessage
from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def _message(
    subject: str,
    *,
    plain: str,
    html: str | None = None,
    attachment: bytes | None = None,
) -> bytes:
    message = EmailMessage()
    message["Date"] = "Tue, 02 Sep 2026 12:34:56 +0000"
    message["From"] = "Reporter <reporter@example.test>"
    message["To"] = "Editor <editor@example.test>, desk@example.test"
    message["Cc"] = "Archive <archive@example.test>"
    message["Subject"] = subject
    message.set_content(plain)
    if html is not None:
        message.add_alternative(html, subtype="html")
    if attachment is not None:
        message.add_attachment(
            attachment,
            maintype="application",
            subtype="octet-stream",
            filename="evidence.bin",
        )
    return message.as_bytes()


def _mbox(message: bytes) -> bytes:
    return b"From reporter@example.test Tue Sep  2 12:34:56 2026\n" + message + b"\n"


def _zip(members: dict[str, bytes]) -> bytes:
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        for logical_path, content in members.items():
            archive.writestr(logical_path, content)
    return payload.getvalue()


def _project(client: TestClient, name: str) -> str:
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _plan(
    client: TestClient,
    project_id: str,
    uploads: list[tuple[str, bytes, str, str]],
    *,
    expand_archive: bool,
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


def _execute_rows(client: TestClient, project_id: str, plan: dict) -> list[dict]:
    response = client.post(
        f"/api/projects/{project_id}/import/bulk/{plan['plan_id']}/execute",
        json={"decisions": {}},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["failed"] == []
    assert len(result["created"]) == 1
    sheet_id = result["created"][0]["sheet_id"]
    data_response = client.get(f"/api/projects/{project_id}/sheets/{sheet_id}/data")
    assert data_response.status_code == 200, data_response.text
    data = data_response.json()
    names = {str(column["id"]): column["name"] for column in data["columns"]}
    return [
        {names[str(column_id)]: value for column_id, value in row["cells"].items()}
        for row in data["rows"]
    ]


def test_direct_and_zip_email_leaves_produce_identical_rows_and_attachments(
    tmp_path: Path,
) -> None:
    attachment = b"\x00exact evidence bytes\xff"
    members = {
        "mail/archive.mbox": _mbox(_message("MBOX message", plain="MBOX plain body")),
        "mail/inbox.eml": _message(
            "EML message",
            plain="EML plain body",
            html="<p>EML <strong>HTML</strong> body</p>",
            attachment=attachment,
        ),
    }
    client = TestClient(create_app(tmp_path / "workspace"))
    direct_project = _project(client, "Direct email")
    zip_project = _project(client, "ZIP email")

    direct_plan = _plan(
        client,
        direct_project,
        [
            (
                Path(path).name,
                content,
                "message/rfc822" if path.endswith(".eml") else "application/mbox",
                path,
            )
            for path, content in reversed(members.items())
        ],
        expand_archive=False,
    )
    zip_plan = _plan(
        client,
        zip_project,
        [
            (
                "mail.zip",
                _zip(dict(reversed(members.items()))),
                "application/zip",
                "mail.zip",
            )
        ],
        expand_archive=True,
    )

    assert direct_plan["questions"] == zip_plan["questions"] == []
    assert direct_plan["proposed_outputs"] == zip_plan["proposed_outputs"]
    direct_rows = _execute_rows(client, direct_project, direct_plan)
    zip_rows = _execute_rows(client, zip_project, zip_plan)
    assert direct_rows == zip_rows
    assert [row["subject"] for row in direct_rows] == ["MBOX message", "EML message"]
    assert [row["source_file"] for row in direct_rows] == [
        "mail/archive.mbox#1",
        "mail/inbox.eml",
    ]
    assert direct_rows[0]["body"] == "MBOX plain body"
    assert direct_rows[0]["html"] == ""
    assert direct_rows[1]["body"] == "EML plain body"
    assert direct_rows[1]["html"] == "<p>EML <strong>HTML</strong> body</p>"
    assert direct_rows[1]["to"] == [
        "Editor <editor@example.test>",
        "desk@example.test",
    ]
    assert direct_rows[1]["cc"] == ["Archive <archive@example.test>"]

    envelope = {
        "blob": hashlib.sha256(attachment).hexdigest(),
        "mime": "application/octet-stream",
        "filename": "evidence.bin",
    }
    assert direct_rows[0]["attachments"] == []
    assert direct_rows[1]["attachments"] == [envelope]
    for project_id in (direct_project, zip_project):
        download = client.get(f"/api/projects/{project_id}/blobs/{envelope['blob']}")
        assert download.status_code == 200
        assert download.content == attachment
