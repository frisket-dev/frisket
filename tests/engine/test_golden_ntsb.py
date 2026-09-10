"""Golden project for NTSB latitude/longitude extraction.

The NTSB pattern is: aviation-accident reports carry a fixed-format location
line, e.g. "Location: 38.123456 N, 121.654321 W". The journalistic payoff is
turning that prose into a numeric lat/lon pair you can map. PDF *ingestion*
(split-to-pages, poppler) is tracked separately under the `import-pdf` gap;
this golden owns the *extraction* half so it is exercised end-to-end the moment
text is available, and stays a regression guard once PDF import lands.

Offline: the computed `regex_extract` + `python` ops are deterministic and
keyless, so this golden runs with no network and no cache.
"""

import pytest

from frisket.engine.store import Project
from tests.action_test_helpers import run_typed_map_request, typed_map_request

# Hand-labeled golden subset: report text -> known-answer (lat, lon).
# Signed decimal degrees: N/E positive, S/W negative.
REPORTS = [
    (
        "NTSB Identifier: WPR23LA001. Location: 38.123456 N, 121.654321 W. "
        "The Cessna 172 departed controlled flight on approach.",
        38.123456,
        -121.654321,
    ),
    (
        "NTSB Identifier: ERA22FA112. Location: 40.712800 N, 074.006000 W. "
        "A Piper PA-28 impacted terrain near the river.",
        40.712800,
        -74.006000,
    ),
    (
        "NTSB Identifier: ANC21LA044. Location: 61.218100 N, 149.900300 W. "
        "Forced landing following a loss of engine power.",
        61.218100,
        -149.900300,
    ),
    (
        "NTSB Identifier: CEN20LA200. Location: 25.761700 N, 080.191800 W. "
        "Hard landing during a training flight; no injuries.",
        25.761700,
        -80.191800,
    ),
]


def _seed(p):
    sheet = p.add_sheet("ntsb")
    col = p.add_column(sheet, "report")
    p.add_rows(sheet, [{"report": r} for r, _, _ in REPORTS], {"report": col})
    return sheet


def _ordered(p, sheet, name):
    """Column values in row position order (golden answers are positional)."""
    col = next(c for c in p.columns(sheet) if c["name"] == name)
    vals = p.get_values(sheet, col["id"])
    row_ids = [r["id"] for r in p.db.execute("SELECT id FROM rows ORDER BY position")]
    return [vals.get(rid) for rid in row_ids]


def _run_python(p, sheet_id, *, source, code, output_name):
    return run_typed_map_request(
        p,
        typed_map_request(
            "map.python",
            sheet_id,
            params={
                "input_columns": [source],
                "code": code,
                "return_schema": {"type": "number"},
                "output_routes": [
                    {
                        "name": output_name,
                        "path": "$",
                        "target": {"kind": "column", "type": "number"},
                    }
                ],
            },
            output_names={output_name: output_name},
            idempotency_key=f"golden-ntsb-{output_name}@1",
        ),
        project_id="golden-ntsb",
    )


def _run_regex(p, sheet_id, *, pattern, output_name):
    return run_typed_map_request(
        p,
        typed_map_request(
            "map.regex_extract",
            sheet_id,
            params={"input_columns": ["report"], "pattern": pattern, "group": 1},
            output_names={"extracted": output_name},
            idempotency_key=f"golden-ntsb-{output_name}@1",
        ),
        project_id="golden-ntsb",
    )


def test_golden_ntsb_latlon_extraction(tmp_path):
    """Extract signed decimal lat/lon from NTSB location lines and check the
    result against hand-known coordinates."""
    p = Project.create(tmp_path / "ntsb.frisket", name="golden-ntsb")
    try:
        sheet = _seed(p)

        # 1) Pull the raw "DD.dddddd H" lat / lon tokens with one regex each.
        prog = _run_regex(
            p,
            sheet,
            pattern=r"Location:\s*([0-9.]+\s*[NS])",
            output_name="lat_raw",
        )
        assert prog.status == "completed", prog.errors
        prog = _run_regex(
            p,
            sheet,
            pattern=r",\s*([0-9.]+\s*[EW])",
            output_name="lon_raw",
        )
        assert prog.status == "completed", prog.errors

        # 2) Convert the hemisphere token to signed decimal degrees in python.
        signed = (
            "tok = row['{src}'].strip()\n"
            "val = float(tok[:-1].strip())\n"
            "result = -val if tok[-1] in ('S', 'W') else val\n"
        )
        prog = _run_python(
            p,
            sheet,
            source="lat_raw",
            code=signed.format(src="lat_raw"),
            output_name="lat",
        )
        assert prog.status == "completed", prog.errors
        prog = _run_python(
            p,
            sheet,
            source="lon_raw",
            code=signed.format(src="lon_raw"),
            output_name="lon",
        )
        assert prog.status == "completed", prog.errors

        got_lat = _ordered(p, sheet, "lat")
        got_lon = _ordered(p, sheet, "lon")
        want_lat = [lat for _, lat, _ in REPORTS]
        want_lon = [lon for _, _, lon in REPORTS]

        for g, w in zip(got_lat, want_lat):
            assert g == pytest.approx(w, abs=1e-6), f"lat {g} != {w}"
        for g, w in zip(got_lon, want_lon):
            assert g == pytest.approx(w, abs=1e-6), f"lon {g} != {w}"

        # Signed-hemisphere sanity: every NTSB report here is in the Western
        # hemisphere, so longitudes must be negative.
        assert all(v < 0 for v in got_lon)
    finally:
        p.close()


def test_golden_ntsb_missing_location_is_per_row_error(tmp_path):
    """A report with no Location line must surface as a per-row failure, not a
    crash — the partial-failure contract the runner promises."""
    p = Project.create(tmp_path / "ntsb2.frisket", name="golden-ntsb-bad")
    try:
        sheet = p.add_sheet("ntsb")
        col = p.add_column(sheet, "report")
        p.add_rows(
            sheet,
            [
                {"report": "Location: 30.000000 N, 090.000000 W. Good row."},
                {"report": "Narrative only; the coordinates were never recorded."},
            ],
            {"report": col},
        )

        _run_regex(
            p,
            sheet,
            pattern=r"Location:\s*([0-9.]+\s*[NS])",
            output_name="lat_raw",
        )

        prog = _run_python(
            p,
            sheet,
            source="lat_raw",
            code="tok = row['lat_raw'].strip()\nresult = float(tok[:-1].strip())\n",
            output_name="lat",
        )
        # one good row, one row whose lat_raw is None -> per-row error
        assert prog.run_id is not None
        run = p.db.execute(
            "SELECT failed_rows FROM runs WHERE id=?", (prog.run_id,)
        ).fetchone()
        assert run["failed_rows"] == 1
    finally:
        p.close()
