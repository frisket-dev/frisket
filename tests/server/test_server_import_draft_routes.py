from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from frisket.server.app import create_app


def test_import_paste_draft_returns_non_mutating_draft_payload(tmp_path: Path) -> None:
    app = create_app(tmp_path / "workspace")
    client = TestClient(app)
    pid = client.post("/api/projects", json={"name": "Draft"}).json()["id"]

    before = client.get(f"/api/projects/{pid}/sheets").json()
    response = client.post(
        f"/api/projects/{pid}/import/drafts/paste",
        json={"raw": 'Name,Name,Score,Notes\nAda,Lovelace,1,"# Heading"\n'},
    )
    after = client.get(f"/api/projects/{pid}/sheets").json()

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["schema_version"] == "frisket.import_draft.v2"
    assert payload["draft_id"].startswith("paste@sha256:")
    assert payload["source_kind"] == "paste"
    assert payload["row_count"] == 1
    assert [column["key"] for column in payload["columns"]] == [
        "Name",
        "Name_2",
        "Score",
        "Notes",
    ]
    assert payload["columns"][2]["type"] == "integer"
    assert payload["columns"][3]["format"] == "markdown"
    assert payload["preview_rows"] == [
        {"Name": "Ada", "Name_2": "Lovelace", "Score": "1", "Notes": "# Heading"}
    ]
    assert before == after == []


def test_import_paste_draft_validation_error_maps_to_400(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Draft"}).json()["id"]

    response = client.post(
        f"/api/projects/{pid}/import/drafts/paste",
        json={"raw": "Name,Score\n"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "paste at least one data row"


def test_paste_keeps_every_value_when_duplicate_headers_have_suffixes(
    tmp_path: Path,
) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Duplicate headers"}).json()["id"]
    raw = "Name\tName\tName_2\nalice\tSMITH\tzz\n"
    response = client.post(
        f"/api/projects/{pid}/import/drafts/paste", json={"raw": raw}
    )
    assert response.status_code == 200, response.text
    draft = response.json()
    names = [column["name"] for column in draft["columns"]]
    assert len(set(names)) == 3
    assert [draft["preview_rows"][0][name] for name in names] == [
        "alice",
        "SMITH",
        "zz",
    ]
    confirmed = client.post(
        f"/api/projects/{pid}/import/drafts/paste/confirm",
        json={
            "raw": raw,
            "draft_id": draft["draft_id"],
            "columns": [
                {"source_name": name, "name": name, "type": "text"} for name in names
            ],
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    sheet_id = confirmed.json()["sheet_id"]
    data = client.get(f"/api/projects/{pid}/sheets/{sheet_id}/data").json()
    assert [list(row["cells"].values()) for row in data["rows"]] == [
        ["alice", "SMITH", "zz"]
    ]


def test_import_paste_draft_has_no_fixed_10000_row_ceiling(tmp_path: Path) -> None:
    """Paste remains a distinct in-memory draft flow, but not a capped one."""
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Large draft"}).json()["id"]
    raw = "name\n" + "".join(f"record-{index}\n" for index in range(10_001))

    response = client.post(
        f"/api/projects/{pid}/import/drafts/paste",
        json={"raw": raw},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["row_count"] == 10_001
    assert len(payload["preview_rows"]) == 20
    assert payload["preview_rows"][-1] == {"name": "record-19"}


# --- _sniff_type: only classify a column numeric when int()/float() would
# hand back the digits the user typed.  Everything below is a shape that
# int()/float() silently rewrites, and every one of them is an identifier
# a journalist imports: ZIP and FIPS codes, case numbers, E.164 phone
# numbers, account numbers longer than a JSON double can hold.

_IDENTIFIER_COLUMNS: list[tuple[str, list[str], str]] = [
    ("zip", ["01234", "02138"], "leading zero: 01234 is not 1234"),
    ("fips", ["06075", "01001"], "leading zero"),
    ("case_no", ["00042", "00043"], "leading zero"),
    ("phone", ["+12125551234", "+13475550000"], "E.164 '+' is not a sign"),
    ("plus_int", ["+5", "+12"], "int() drops the '+'"),
    ("unit", ["1e5", "2E3"], "exponent notation is not a spelling we keep"),
    ("account", ["123456789012345678", "123456789012345679"], "beyond 2**53"),
    ("literal", ["1_000", "2_000"], "Python literal syntax is not CSV"),
    ("missing", ["nan", "inf"], "float() invents NaN/Infinity"),
    ("trailing_dot", ["5.", "6."], "not a numeral we round-trip"),
]

_NUMERIC_COLUMNS: list[tuple[str, list[str], str]] = [
    ("score", ["1", "42"], "integer"),
    ("year", ["1999", "2026"], "integer"),
    ("count", ["0", "7"], "integer"),
    ("delta", ["-5", "-12"], "integer"),
    ("grouped", ["1,234", "5,678"], "integer"),
    ("price", ["1.50", "2.00"], "number"),
    ("ratio", ["0.5", "-0.25"], "number"),
    ("share", [".5", ".25"], "number"),
    ("padded", [" 12 ", " 13 "], "integer"),
]


def test_sniff_type_keeps_identifier_shaped_columns_as_text() -> None:
    from frisket.server.services.import_inference import infer_column_type

    observed = {
        name: infer_column_type(name, values) for name, values, _ in _IDENTIFIER_COLUMNS
    }
    assert observed == {name: "text" for name, _, _ in _IDENTIFIER_COLUMNS}, (
        "these shapes lose data when coerced: "
        + "; ".join(f"{name}: {why}" for name, _, why in _IDENTIFIER_COLUMNS)
    )


def test_sniff_type_still_recognizes_real_numbers() -> None:
    from frisket.server.services.import_inference import infer_column_type

    observed = {
        name: infer_column_type(name, values) for name, values, _ in _NUMERIC_COLUMNS
    }
    assert observed == {name: expected for name, _, expected in _NUMERIC_COLUMNS}


def test_sniff_type_honors_the_semicolon_decimal_convention() -> None:
    from frisket.server.services.import_inference import infer_column_type

    assert (
        infer_column_type("price", ["1.234,56", "9,50"], decimal_separator=",")
        == "number"
    )
    assert (
        infer_column_type("count", ["1.234", "9"], decimal_separator=",") == "integer"
    )
    assert infer_column_type("zip", ["01234", "02138"], decimal_separator=",") == "text"


def test_import_paste_draft_keeps_zip_codes_as_text(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path / "workspace"))
    pid = client.post("/api/projects", json={"name": "Draft"}).json()["id"]

    response = client.post(
        f"/api/projects/{pid}/import/drafts/paste",
        json={"raw": "name,zip,score\nAlice,01234,7\nBob,02138,9\n"},
    )

    assert response.status_code == 200, response.text
    columns = {c["name"]: c["type"] for c in response.json()["columns"]}
    assert columns == {"name": "text", "zip": "text", "score": "integer"}
    assert response.json()["preview_rows"][0]["zip"] == "01234"
