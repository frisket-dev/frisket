"""Markdown-cell detection, persistence, and rendering contracts.

Backend contract: columns whose text is clearly markdown get format='markdown'
sniffed on import; the format is settable and clearable via the v1
column.patch action; the op lands in history and undoes/redoes. The frontend
rendering half is covered by web/tests/e2e/markdown.spec.ts.
"""

from fastapi.testclient import TestClient

from frisket.ai.llm import ModelRouter
from frisket.server.app import create_app
from frisket.server.services.import_inference import _looks_markdown, sniff_markdown
from http_test_helpers import (
    post_column_patch_as_v1_action,
    post_operation_redo_as_v1_action,
    post_operation_undo_as_v1_action,
)

MD_CSV = (
    "title,body\n"
    'one,"# Heading\n\n**bold** and a [link](https://example.com)\n\n- item"\n'
    'two,"## Another\n\n*italic* text with `code`\n\n1. listed"\n'
)


def _client(tmp_path) -> TestClient:
    router = ModelRouter(cache=None, cache_mode="off")
    return TestClient(create_app(tmp_path / "ws", router=router))


def _import(client, pid: str, csv: str) -> int:
    r = client.post(
        f"/api/projects/{pid}/import/csv", files={"file": ("t.csv", csv, "text/csv")}
    )
    assert r.status_code == 200, r.text
    return r.json()["sheet_id"]


def _cols(client, pid: str, sheet_id: int) -> dict[str, dict]:
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    return {c["name"]: c for c in data["columns"]}


# ---------------------------------------------------------------------------
# Sniffer heuristics


def test_looks_markdown_strong_and_weak_signals():
    assert _looks_markdown("# A heading")
    assert _looks_markdown("```python\nprint('hi')\n```")
    # two distinct weak signals qualify ...
    assert _looks_markdown("**bold** with a [link](https://x.com)")
    assert _looks_markdown("- one\n- two\n\nsome `code` too")
    # ... a single weak signal does not (prose uses asterisks/underscores)
    assert not _looks_markdown("see the **quarterly** numbers")
    assert not _looks_markdown("plain sentence, nothing else")
    assert not _looks_markdown("math like 3*4*5 shouldn't count as italics")


def test_sniff_markdown_tolerates_a_few_plain_rows():
    md = "# H\n\n**b** and [l](https://x.com)"
    assert sniff_markdown([md, md, md, md, "plain row"])  # 4/5 = 0.8
    assert not sniff_markdown([md, "plain", "plain", "also plain"])
    assert not sniff_markdown([])
    assert not sniff_markdown([None, ""])


# ---------------------------------------------------------------------------
# Import sniffing + the v1 column.patch round-trip


def test_csv_import_sniffs_markdown_column_only(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Md"}).json()["id"]
    sheet_id = _import(client, pid, MD_CSV)
    cols = _cols(client, pid, sheet_id)
    assert cols["body"]["format"] == "markdown"
    assert cols["title"]["format"] is None  # plain text stays default


def test_patch_column_format_set_clear_and_validate(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Md"}).json()["id"]
    sheet_id = _import(client, pid, "note\nhello\nworld\n")
    col = _cols(client, pid, sheet_id)["note"]
    assert col["format"] is None

    # set
    r = post_column_patch_as_v1_action(client, pid, col["id"], {"format": "markdown"})
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
    assert _cols(client, pid, sheet_id)["note"]["format"] == "markdown"

    # clear (explicit null)
    r = post_column_patch_as_v1_action(client, pid, col["id"], {"format": None})
    assert r.status_code == 200
    assert _cols(client, pid, sheet_id)["note"]["format"] is None

    # unknown format is rejected, column untouched
    r = post_column_patch_as_v1_action(client, pid, col["id"], {"format": "sparkles"})
    assert r.status_code == 400
    assert r.json()["errors"][0]["code"] == "invalid_column_format"
    assert _cols(client, pid, sheet_id)["note"]["format"] is None

    # unknown columns fail through the v1 action result envelope
    r = post_column_patch_as_v1_action(client, pid, 999999, {"format": "markdown"})
    assert r.status_code == 400
    assert r.json()["errors"][0]["code"] == "invalid_column_ref"


def test_format_patch_is_undoable(tmp_path):
    client = _client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Md"}).json()["id"]
    sheet_id = _import(client, pid, "note\nhello\nworld\n")
    col = _cols(client, pid, sheet_id)["note"]
    patch = post_column_patch_as_v1_action(
        client, pid, col["id"], {"format": "markdown"}
    )
    assert patch.status_code == 200, patch.text
    patch_op_id = patch.json()["op_ids"][0]
    assert _cols(client, pid, sheet_id)["note"]["format"] == "markdown"

    # the op is in history with a human label
    history = client.get(f"/api/projects/{pid}/history").json()["ops"]
    labels = [o["label"] for o in history]
    assert any("column.patch note format markdown" in (lbl or "") for lbl in labels)

    undo = post_operation_undo_as_v1_action(client, pid, expected_op_id=patch_op_id)
    assert undo.status_code == 200, undo.text
    assert undo.json()["schema_version"] == "frisket.action_result.v1"
    assert undo.json()["status"] == "completed"
    assert _cols(client, pid, sheet_id)["note"]["format"] is None
    redo = post_operation_redo_as_v1_action(client, pid, expected_op_id=patch_op_id)
    assert redo.status_code == 200, redo.text
    assert redo.json()["schema_version"] == "frisket.action_result.v1"
    assert redo.json()["status"] == "completed"
    assert _cols(client, pid, sheet_id)["note"]["format"] == "markdown"
