"""Shared work-log export payloads and renderers."""

from __future__ import annotations

import html
import json
import textwrap
from typing import Any

from frisket.authoring.action_metadata import (
    action_metadata_for_action_kind,
    run_row_action_kind,
)
from frisket.engine.receipt_index import work_log_receipt_summaries
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore, project_model_provenance_summary


def export_value(value: Any, *, limit: int = 180) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (int, float)):
        text = str(value)
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    text = text.replace("\r", " ").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."


def markdown_table_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", "<br>")
    )


def _decode_run_params(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_work_log_payload(
    project: Project,
    project_id: str,
    *,
    include_receipts: bool = True,
) -> dict[str, Any]:
    sheets = project.sheets()
    history_rows = project.history()
    run_rows = project.db.execute("SELECT * FROM runs ORDER BY id").fetchall()
    # Touched models and total cost share the provenance manifest's builder so
    # the work log can never disagree with it.
    provenance = project_model_provenance_summary(project)
    receipts = _receipt_summaries(project) if include_receipts else []
    action_kind_by_operation_id = _action_kind_by_operation_id(receipts)
    project_name = str(project.get_meta("name", project_id) or project_id)
    runs = []
    for run in run_rows:
        action_metadata = action_metadata_for_action_kind(run_row_action_kind(run))
        # An export is a re-emittable copy of the project, so it reads what the
        # normal read path reads: no undo/deleted rows, no hidden columns.
        output_cols = [
            row["name"]
            for row in project.db.execute(
                "SELECT DISTINCT c.name FROM results res "
                "JOIN columns c ON c.id=res.column_id "
                "WHERE res.run_id=? AND c.hidden=0 ORDER BY c.position",
                (run["id"],),
            )
        ]
        example_rows = project.db.execute(
            "SELECT rows.id AS row_id, rows.position, c.id AS column_id, "
            "c.name AS column_name, res.error "
            "FROM results res "
            "JOIN rows ON rows.id=res.row_id "
            "JOIN columns c ON c.id=res.column_id "
            "WHERE res.run_id=? AND rows.hidden=0 AND c.hidden=0 "
            "ORDER BY rows.position, c.position, c.id LIMIT 8",
            (run["id"],),
        ).fetchall()
        coordinates = [
            (int(run["id"]), int(row["row_id"]), int(row["column_id"]))
            for row in example_rows
            if row["error"] is None
        ]
        resolved = RunResultStore(project).decoded_result_rows(
            coordinates, tolerate_decode_errors=True
        )
        examples = []
        for row in example_rows:
            coordinate = (
                int(run["id"]),
                int(row["row_id"]),
                int(row["column_id"]),
            )
            result = resolved.get(coordinate) if row["error"] is None else None
            value = result["value"] if result is not None else None
            examples.append(
                {
                    "row": int(row["position"]) + 1,
                    "column": row["column_name"],
                    "value": export_value(value),
                    "error": row["error"],
                }
            )
        run_spec = _decode_run_params(run["params"])
        runs.append(
            {
                "id": run["id"],
                **action_metadata,
                "model": run["model"] or "local/non-model",
                "completed_rows": run["completed_rows"] or 0,
                "total_rows": run["total_rows"] or 0,
                "failed_rows": run["failed_rows"] or 0,
                "cost": float(run["cost_actual"] or 0.0),
                "outputs": output_cols,
                "prompt": run_spec.get("prompt"),
                "examples": examples,
            }
        )
    return {
        "project_id": project_id,
        "project_name": project_name,
        "export_action_kind": "export.work_log",
        "sheets": [
            {
                "name": sheet["name"],
                "rows": project.row_count(sheet["id"]),
                "columns": len(project.columns(sheet["id"])),
            }
            for sheet in sheets
        ],
        "operations": [
            {
                "id": operation["id"],
                "kind": operation["kind"],
                "label": operation["label"],
                "action_kind": action_kind_by_operation_id.get(int(operation["id"])),
                "status": operation["status"],
                "created_at": operation["created_at"],
            }
            for operation in history_rows
        ],
        "runs": runs,
        "receipts": receipts,
        "touched_models": provenance.touched,
        "total_cost": float(provenance.total_cost),
    }


def _action_kind_by_operation_id(receipts: list[dict[str, Any]]) -> dict[int, str]:
    action_kind_by_id: dict[int, str] = {}
    for receipt in receipts:
        action_kind = str(receipt.get("action_kind") or "")
        if not action_kind:
            continue
        for operation_id in receipt.get("op_ids") or []:
            try:
                action_kind_by_id[int(operation_id)] = action_kind
            except (TypeError, ValueError):
                continue
    return action_kind_by_id


def _receipt_summaries(project: Project) -> list[dict[str, Any]]:
    return work_log_receipt_summaries(project)


def _markdown_runs_section(runs: list[dict[str, Any]]) -> list[str]:
    lines = ["", "## Runs", ""]
    if not runs:
        lines.append("_No runs recorded._")
        return lines
    lines.extend(
        [
            "| Run | Action | Model | Rows | Failed | Cost | Outputs |",
            "|---:|---|---|---:|---:|---:|---|",
        ]
    )
    for run in runs:
        lines.append(
            "| {id} | {action} | {model} | {done}/{total} | {failed} | "
            "${cost:.4f} | {outputs} |".format(
                id=run["id"],
                action=markdown_table_cell(run["action_kind"]),
                model=markdown_table_cell(run["model"] or "local/non-model"),
                done=run["completed_rows"],
                total=run["total_rows"],
                failed=run["failed_rows"],
                cost=run["cost"],
                outputs=markdown_table_cell(", ".join(run["outputs"]) or "none"),
            )
        )
        if run.get("prompt"):
            lines.extend(
                [
                    "",
                    f"Prompt for run {run['id']}:",
                    "",
                    "> " + str(run["prompt"]).replace("\n", "\n> "),
                    "",
                ]
            )
        if run["examples"]:
            lines.extend(
                [
                    "",
                    f"Example changes for run {run['id']}:",
                    "",
                    "| Row | Column | Value |",
                    "|---:|---|---|",
                ]
            )
            for ex in run["examples"]:
                value = f"ERROR: {ex['error']}" if ex["error"] else ex["value"]
                lines.append(
                    f"| {ex['row']} | {markdown_table_cell(ex['column'])} | "
                    f"{markdown_table_cell(value)} |"
                )
    return lines


def _markdown_receipts_section(receipts: list[dict[str, Any]]) -> list[str]:
    lines = ["", "## Action Receipts", ""]
    if not receipts:
        lines.append("_No action receipts recorded._")
        return lines
    lines.extend(
        [
            "| Receipt | Action | Status | Ops | Outputs | Records |",
            "|---|---|---|---|---|---|",
        ]
    )
    for receipt in receipts:
        lines.append(
            f"| {markdown_table_cell(receipt['id'])} | "
            f"{markdown_table_cell(receipt['action_kind'])} | "
            f"{markdown_table_cell(receipt['status'])} | "
            f"{markdown_table_cell(', '.join(str(item) for item in receipt['op_ids']) or 'none')} | "
            f"{markdown_table_cell(', '.join(receipt['outputs']) or 'none')} | "
            f"{markdown_table_cell(', '.join(receipt['records']) or 'none')} |"
        )
    return lines


def work_log_markdown(payload: dict[str, Any]) -> str:
    lines = [
        f"# Work log: {payload['project_name']}",
        "",
        "## Summary",
        "",
        f"- Project id: `{payload['project_id']}`",
        f"- Export action: `{payload.get('export_action_kind', 'export.work_log')}`",
        f"- Sheets: {len(payload['sheets'])}",
        f"- Operations: {len(payload['operations'])}",
        f"- Runs: {len(payload['runs'])}",
        f"- Receipts: {len(payload.get('receipts', []))}",
        f"- Touched models: {', '.join(payload['touched_models']) if payload['touched_models'] else 'none'}",
        f"- Recorded model cost: ${float(payload['total_cost']):.4f}",
        "",
        "## Sheets",
        "",
        "| Sheet | Rows | Columns |",
        "|---|---:|---:|",
    ]
    for sheet in payload["sheets"]:
        lines.append(
            f"| {markdown_table_cell(sheet['name'])} | {sheet['rows']} | {sheet['columns']} |"
        )

    lines.extend(["", "## Operation History", ""])
    if payload["operations"]:
        lines.extend(
            ["| # | Kind | Action | Status | Created |", "|---:|---|---|---|---|"]
        )
        for operation in payload["operations"]:
            action_label = operation.get("action_kind") or operation["label"]
            lines.append(
                f"| {operation['id']} | {markdown_table_cell(operation['kind'])} | "
                f"{markdown_table_cell(action_label)} | "
                f"{markdown_table_cell(operation['status'])} | "
                f"{markdown_table_cell(operation['created_at'])} |"
            )
    else:
        lines.append("_No operations recorded._")

    lines.extend(_markdown_runs_section(payload["runs"]))
    lines.extend(_markdown_receipts_section(payload.get("receipts") or []))
    return "\n".join(lines) + "\n"


def html_cell(value: Any) -> str:
    return html.escape(export_value(value)).replace("\n", "<br>")


def _html_escape(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _html_runs_section(runs: list[dict[str, Any]]) -> list[str]:
    rows = ["<h2>Runs</h2>"]
    if not runs:
        rows.append('<p class="muted">No runs recorded.</p>')
        return rows
    rows.append(
        "<table><thead><tr><th>Run</th><th>Action</th><th>Model</th><th>Rows</th><th>Failed</th><th>Cost</th><th>Outputs</th></tr></thead><tbody>"
    )
    for run in runs:
        rows.append(
            f"<tr><td>{run['id']}</td><td>{_html_escape(run['action_kind'])}</td><td>{_html_escape(run['model'])}</td>"
            f"<td>{run['completed_rows']}/{run['total_rows']}</td><td>{run['failed_rows']}</td>"
            f"<td>${run['cost']:.4f}</td><td>{_html_escape(', '.join(run['outputs']) or 'none')}</td></tr>"
        )
    rows.append("</tbody></table>")
    for run in runs:
        if run.get("prompt"):
            rows.append(
                f"<h3>Prompt for run {run['id']}</h3><pre>{_html_escape(run['prompt'])}</pre>"
            )
        if run["examples"]:
            rows.append(f"<h3>Example changes for run {run['id']}</h3>")
            rows.append(
                "<table><thead><tr><th>Row</th><th>Column</th><th>Value</th></tr></thead><tbody>"
            )
            for ex in run["examples"]:
                value = f"ERROR: {ex['error']}" if ex["error"] else ex["value"]
                cls = ' class="error"' if ex["error"] else ""
                rows.append(
                    f"<tr><td>{ex['row']}</td><td>{_html_escape(ex['column'])}</td><td{cls}>{html_cell(value)}</td></tr>"
                )
            rows.append("</tbody></table>")
    return rows


def _html_receipts_section(receipts: list[dict[str, Any]]) -> list[str]:
    rows = ["<h2>Action Receipts</h2>"]
    if not receipts:
        rows.append('<p class="muted">No action receipts recorded.</p>')
        return rows
    rows.append(
        "<table><thead><tr><th>Receipt</th><th>Action</th><th>Status</th><th>Ops</th><th>Outputs</th><th>Records</th></tr></thead><tbody>"
    )
    for receipt in receipts:
        rows.append(
            f"<tr><td>{_html_escape(receipt['id'])}</td><td>{_html_escape(receipt['action_kind'])}</td>"
            f"<td>{_html_escape(receipt['status'])}</td>"
            f"<td>{_html_escape(', '.join(str(item) for item in receipt['op_ids']) or 'none')}</td>"
            f"<td>{_html_escape(', '.join(receipt['outputs']) or 'none')}</td>"
            f"<td>{_html_escape(', '.join(receipt['records']) or 'none')}</td></tr>"
        )
    rows.append("</tbody></table>")
    return rows


def work_log_html(payload: dict[str, Any]) -> str:
    rows = [
        "<!doctype html>",
        '<html><head><meta charset="utf-8">',
        f"<title>Work log: {_html_escape(payload['project_name'])}</title>",
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "color:#1f2937;margin:32px;line-height:1.45}"
        "h1{font-size:28px;margin:0 0 8px}h2{font-size:18px;margin-top:28px}"
        "table{border-collapse:collapse;width:100%;margin:10px 0 18px}"
        "th,td{border:1px solid #d7dde7;padding:7px 9px;text-align:left;vertical-align:top}"
        "th{background:#f3f5f8;font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#5b6575}"
        ".summary{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;margin:18px 0}"
        ".metric{background:#f7f9fc;border:1px solid #dfe5ee;border-radius:6px;padding:10px}"
        ".metric strong{display:block;font-size:20px}.muted{color:#667085}.error{color:#b42318}"
        "pre{white-space:pre-wrap;background:#f7f9fc;border:1px solid #dfe5ee;border-radius:6px;padding:10px}"
        "</style></head><body>",
        f"<h1>Work log: {_html_escape(payload['project_name'])}</h1>",
        f'<p class="muted">Project id: <code>{_html_escape(payload["project_id"])}</code></p>',
        '<section class="summary">',
        f'<div class="metric"><span>Sheets</span><strong>{len(payload["sheets"])}</strong></div>',
        f'<div class="metric"><span>Operations</span><strong>{len(payload["operations"])}</strong></div>',
        f'<div class="metric"><span>Runs</span><strong>{len(payload["runs"])}</strong></div>',
        f'<div class="metric"><span>Models</span><strong>{len(payload["touched_models"])}</strong></div>',
        f'<div class="metric"><span>Cost</span><strong>${float(payload["total_cost"]):.4f}</strong></div>',
        "</section>",
        "<h2>Sheets</h2><table><thead><tr><th>Sheet</th><th>Rows</th><th>Columns</th></tr></thead><tbody>",
    ]
    for sheet in payload["sheets"]:
        rows.append(
            f"<tr><td>{_html_escape(sheet['name'])}</td><td>{sheet['rows']}</td><td>{sheet['columns']}</td></tr>"
        )
    rows.extend(["</tbody></table>", "<h2>Operation History</h2>"])
    if payload["operations"]:
        rows.append(
            "<table><thead><tr><th>#</th><th>Kind</th><th>Action</th><th>Status</th><th>Created</th></tr></thead><tbody>"
        )
        for operation in payload["operations"]:
            action_label = operation.get("action_kind") or operation["label"]
            rows.append(
                f"<tr><td>{operation['id']}</td><td>{_html_escape(operation['kind'])}</td><td>{_html_escape(action_label)}</td>"
                f"<td>{_html_escape(operation['status'])}</td><td>{_html_escape(operation['created_at'])}</td></tr>"
            )
        rows.append("</tbody></table>")
    else:
        rows.append('<p class="muted">No operations recorded.</p>')

    rows.extend(_html_runs_section(payload["runs"]))
    rows.extend(_html_receipts_section(payload.get("receipts") or []))
    rows.append("</body></html>")
    return "\n".join(rows)


def work_log_pdf_lines(payload: dict[str, Any]) -> list[str]:
    lines = [
        f"Work log: {payload['project_name']}",
        f"Project id: {payload['project_id']}",
        "",
        (
            f"Summary: {len(payload['sheets'])} sheets, "
            f"{len(payload['operations'])} operations, {len(payload['runs'])} runs, "
            f"${float(payload['total_cost']):.4f} recorded model cost"
        ),
        "",
        "Sheets",
    ]
    for sheet in payload["sheets"]:
        lines.append(
            f"- {sheet['name']}: {sheet['rows']} rows, {sheet['columns']} columns"
        )
    lines.extend(["", "Operation History"])
    if payload["operations"]:
        for operation in payload["operations"]:
            action_label = operation.get("action_kind") or operation["label"]
            lines.append(
                f"- #{operation['id']} {operation['kind']}: {action_label} ({operation['status']})"
            )
    else:
        lines.append("- No operations recorded.")
    lines.extend(["", "Runs"])
    if payload["runs"]:
        for run in payload["runs"]:
            lines.append(
                f"- Run {run['id']} {run['action_kind']} via {run['model']}: "
                f"{run['completed_rows']}/{run['total_rows']} rows, "
                f"{run['failed_rows']} failed, ${run['cost']:.4f}, "
                f"outputs: {', '.join(run['outputs']) or 'none'}"
            )
            if run.get("prompt"):
                lines.append(f"  Prompt: {export_value(run['prompt'], limit=400)}")
            for ex in run["examples"][:5]:
                value = f"ERROR: {ex['error']}" if ex["error"] else ex["value"]
                lines.append(
                    f"  Example row {ex['row']}, {ex['column']}: {export_value(value, limit=220)}"
                )
    else:
        lines.append("- No runs recorded.")
    receipts = payload.get("receipts") or []
    lines.extend(["", "Action Receipts"])
    if receipts:
        for receipt in receipts:
            operation_ids = ", ".join(str(item) for item in receipt["op_ids"]) or "none"
            outputs = ", ".join(receipt["outputs"]) or "none"
            records = ", ".join(receipt["records"]) or "none"
            lines.append(
                f"- {receipt['id']} {receipt['action_kind']} "
                f"({receipt['status']}), ops: {operation_ids}, outputs: {outputs}, "
                f"records: {records}"
            )
    else:
        lines.append("- No action receipts recorded.")
    return lines


def pdf_escape(text: str) -> str:
    safe = text.encode("latin-1", errors="replace").decode("latin-1")
    return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def simple_text_pdf(lines: list[str]) -> bytes:
    wrapped: list[str] = []
    for line in lines:
        if line == "":
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(line, width=92, replace_whitespace=False) or [""])

    pages: list[str] = []
    current: list[str] = []
    max_lines = 50
    for line in wrapped:
        if len(current) >= max_lines:
            pages.append("\n".join(current))
            current = []
        current.append(line)
    pages.append("\n".join(current))

    objects: list[bytes] = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_refs: list[str] = []
    for page_text in pages:
        content_parts = ["BT", "/F1 10 Tf", "50 742 Td", "14 TL"]
        for line in page_text.splitlines():
            content_parts.append(f"({pdf_escape(line)}) Tj")
            content_parts.append("T*")
        content_parts.append("ET")
        content = "\n".join(content_parts).encode("latin-1", errors="replace")
        content_obj_id = len(objects) + 2
        page_obj_id = len(objects) + 1
        page_refs.append(f"{page_obj_id} 0 R")
        objects.append(
            (
                "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_obj_id} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(content)).encode("ascii")
            + b" >>\nstream\n"
            + content
            + b"\nendstream"
        )
    objects[1] = (
        f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(page_refs)} >>"
    ).encode("ascii")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")
    xref_offset = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(out)
