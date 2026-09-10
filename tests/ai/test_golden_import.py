"""Golden project for the import gauntlet and a performance-scale ingest.

Drives the real HTTP import surface (CSV + XLSX) through the FastAPI app so a
regression in column-type sniffing, header handling, or row counts trips the
gate. Plus a perf smoke that ingests a five-figure row count straight through
the store to keep the bulk-insert path honest (the 100k-row run on the real
backend is the live/manual extension of this).

Offline: no network, no model keys — import is deterministic.
"""

import io
import time

import pytest

from frisket.ai.llm import ModelRouter
from frisket.engine.store import Project

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from helpers import make_client  # noqa: E402


@pytest.fixture
def client(tmp_path):
    return make_client(tmp_path, router=ModelRouter(cache=None, cache_mode="off"))


def _new_project(client, name="gauntlet"):
    return client.post("/api/projects", json={"name": name}).json()["id"]


CSV_BODY = (
    "name,age,city\n"
    "Linda Reyes,34,Sacramento\n"
    "Marcus Hale,51,Brooklyn\n"
    "Priya Anand,29,Austin\n"
)


def test_golden_import_csv_gauntlet(client):
    """CSV import lands a sheet with the right rows + columns."""
    pid = _new_project(client)
    r = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("people.csv", CSV_BODY, "text/csv")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"] == 3
    assert set(body["columns"]) == {"name", "age", "city"}


def test_golden_import_empty_csv_is_rejected(client):
    """A header-only / empty CSV is a 400, not a silent empty sheet."""
    pid = _new_project(client)
    r = client.post(
        f"/api/projects/{pid}/import/csv",
        files={"file": ("empty.csv", "name,age,city\n", "text/csv")},
    )
    assert r.status_code == 400


def _xlsx_bytes(rows):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_golden_import_xlsx_gauntlet(client):
    """XLSX import reads the first worksheet, header row first."""
    pid = _new_project(client)
    data = _xlsx_bytes(
        [
            ["sku", "qty", "price"],
            ["A-100", 12, 4.50],
            ["B-220", 7, 19.99],
        ]
    )
    r = client.post(
        f"/api/projects/{pid}/import/xlsx",
        files={
            "file": (
                "stock.xlsx",
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rows"] == 2
    assert set(body["columns"]) == {"sku", "qty", "price"}


def test_golden_import_perf_bulk_rows(tmp_path):
    """Perf smoke: bulk-ingest a five-figure row count and assert the
    single-transaction add_rows path stays well under budget. The headline
    100k-row run on the real backend is the manual/live extension of this."""
    n = 20_000
    p = Project.create(tmp_path / "perf.frisket", name="golden-perf")
    try:
        sheet = p.add_sheet("big")
        cols = {"id": p.add_column(sheet, "id"), "val": p.add_column(sheet, "val")}
        records = [{"id": str(i), "val": f"row-{i}"} for i in range(n)]

        t0 = time.perf_counter()
        p.add_rows(sheet, records, cols)
        elapsed = time.perf_counter() - t0

        (count,) = p.db.execute(
            "SELECT COUNT(*) FROM rows WHERE sheet_id=?", (sheet,)
        ).fetchone()
        assert count == n
        # Generous ceiling: a regression that drops the single-transaction
        # bulk path (e.g. per-row commits) blows straight past this.
        assert elapsed < 30.0, f"bulk insert of {n} rows took {elapsed:.1f}s"
    finally:
        p.close()
