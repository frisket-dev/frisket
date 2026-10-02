"""Exercise real bulk ingress while measuring its owned source handles."""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from frisket.server.app import create_app
from frisket.server.services import import_bulk_sources


@pytest.mark.parametrize("count", [16, 160])
@pytest.mark.parametrize("kind", ["files", "eml", "mbox"])
def test_bulk_source_handles_stay_bounded_as_inventory_grows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, count: int
) -> None:
    original_open = import_bulk_sources.open_verified_source
    live = peak = opened = 0

    class CountedSource:
        def __init__(self, stream):
            nonlocal live, peak, opened
            self.stream = stream
            self.closed = False
            live += 1
            opened += 1
            peak = max(peak, live)

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def close(self):
            nonlocal live
            if not self.closed:
                self.closed = True
                try:
                    self.stream.close()
                finally:
                    live -= 1

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            self.close()

    monkeypatch.setattr(
        import_bulk_sources,
        "open_verified_source",
        lambda root, item: CountedSource(original_open(root, item)),
    )
    if kind == "files":
        content, suffix, mime = b"same original bytes", "txt", "text/plain"
    else:
        message = EmailMessage()
        message["From"] = "reporter@example.test"
        message["To"] = "editor@example.test"
        message["Subject"] = "Evidence"
        message.set_content("Same content, different source occurrences.")
        content, suffix, mime = message.as_bytes(), kind, "message/rfc822"
        if kind == "mbox":
            content = (
                b"From reporter@example.test Tue Sep  2 12:34:56 2026\n"
                + content
                + b"\n"
            )
            mime = "application/mbox"

    client = TestClient(create_app(tmp_path / "workspace"))
    created = client.post("/api/projects", json={"name": "Handle bounds"})
    assert created.status_code == 200, created.text
    project_id = created.json()["id"]
    base = f"/api/projects/{project_id}"
    parts = []
    for index in reversed(range(count)):
        filename = f"{index:04d}.{suffix}"
        parts.extend(
            [
                ("files", (filename, content, mime)),
                ("logical_paths", (None, f"folder/{filename}")),
            ]
        )
    parts.append(("expand_archive", (None, "false")))
    plan = client.post(f"{base}/import/bulk/plan", files=parts)
    assert plan.status_code == 200, plan.text
    response = client.post(
        f"{base}/import/bulk/{plan.json()['plan_id']}/execute",
        json={"decisions": {}},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["failed"] == []
    assert len(result["created"]) == 1
    sheet = result["created"][0]
    assert sheet["rows"] == count
    assert opened == count
    assert live == 0
    assert peak == 1

    # Identical bytes must not collapse distinct source occurrences.
    response = client.get(f"{base}/sheets/{sheet['sheet_id']}/data")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["total"] == count
    if kind != "files":
        column = next(c for c in data["columns"] if c["name"] == "source_file")
        paths = [row["cells"][str(column["id"])] for row in data["rows"]]
        assert paths == sorted(paths)
        assert len(set(paths)) == len(paths)
