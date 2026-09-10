import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import type { Page } from '@playwright/test';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

export interface SeededEvidenceCell {
  rowId: number;
  columnId: number;
  stableId: string;
}

export interface StubbedV1ActionRun {
  legacyPosts: Array<Record<string, unknown>>;
  v1Posts: Array<Record<string, unknown>>;
  statusPolls: string[];
}

/** Dismiss the overlay action drawer (workbench-ia-action-drawer-v1) so it stops
 *  intercepting pointer events over the grid. Falls back to the legacy resident
 *  panel's "Back to actions" affordance. */
export async function closeActionFormIfOpen(page: Page): Promise<void> {
  const closeButton = page.getByTestId('action-drawer-close');
  if (await closeButton.isVisible().catch(() => false)) {
    await closeButton.click();
    await page.getByTestId('action-drawer').waitFor({ state: 'hidden' }).catch(() => undefined);
    return;
  }
  const backButton = page.getByLabel('Back to actions');
  if (await backButton.isVisible().catch(() => false)) {
    await backButton.click();
  }
}

export async function hideActionsPanelIfOpen(page: Page): Promise<void> {
  // The overlay drawer covers the far-right grid; closing it clears the
  // interception. If it is already closed this is a no-op.
  const closeButton = page.getByTestId('action-drawer-close');
  if (await closeButton.isVisible().catch(() => false)) {
    await closeButton.click();
    await page.getByTestId('action-drawer').waitFor({ state: 'hidden' }).catch(() => undefined);
    return;
  }
  const hideButton = page.getByRole('button', { name: 'Hide actions panel' });
  if (await hideButton.isVisible().catch(() => false)) {
    await hideButton.click().catch(() => undefined);
  }
}

export async function stubV1ActionRun({
  page,
  pid,
  runId = 9801,
}: {
  page: Page;
  pid: string;
  runId?: number;
}): Promise<StubbedV1ActionRun> {
  const legacyPosts: Array<Record<string, unknown>> = [];
  const v1Posts: Array<Record<string, unknown>> = [];
  const statusPolls: string[] = [];
  await page.route(`**/api/projects/${pid}/run`, async (route) => {
    legacyPosts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'investigative action UI must use v1 actions' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    v1Posts.push(body);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: body.action_id ?? body.kind, action_id: `act-${String(body.action_id ?? body.kind)}` },
        status: 'completed',
        project_id: pid,
        run_id: runId,
        receipt_id: `receipt-${String(body.action_id ?? body.kind)}`,
        outputs: [],
        warnings: [],
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/estimate`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    const action = (body.action ?? {}) as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_estimate_result.v1',
        action: { kind: action.kind ?? 'investigative.action', action_id: 'estimate-investigative' },
        project_id: pid,
        estimate: {
          rows: 1,
          cost: 1.25,
          cost_source: 'estimated',
          billed_cost: 1_250_000,
          policy_id: 'frisket.pricing.identity.v1',
          avg_input_tokens: 12,
        },
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/*/status`, async (route) => {
    statusPolls.push(route.request().url());
    const pathParts = new URL(route.request().url()).pathname.split('/');
    const id = Number(pathParts[pathParts.length - 2] ?? runId);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.actions.v1',
        action: 'run_status',
        project_id: pid,
        run: {
          id,
          sheet_id: 1,
          action_kind: 'investigative.action',
          action_name: 'Investigative action',
          status: 'completed',
          total_rows: 1,
          completed_rows: 1,
          failed_rows: 0,
          cost_actual: 0,
          cost_estimate: 0,
          public_status: {
            run_id: id,
            action_kind: 'investigative.action',
            action_name: 'Investigative action',
            status: 'completed',
            total: 1,
            completed: 1,
            failed: 0,
            cost: 0,
            live: false,
          },
        },
      }),
    });
  });
  return { legacyPosts, v1Posts, statusPolls };
}

export function seedGeneratedCellEvidence({
  pid,
  sheetId,
  outputColumnName,
  outputValue,
  producerKind,
}: {
  pid: string;
  sheetId: number;
  outputColumnName: string;
  outputValue: string;
  producerKind: string;
}): SeededEvidenceCell {
  return seedEvidence({
    pid,
    sheetId,
    mode: 'generated',
    columnName: outputColumnName,
    value: outputValue,
    producerKind,
  });
}

export function seedSourceCellEvidence({
  pid,
  sheetId,
  columnName,
  producerKind,
}: {
  pid: string;
  sheetId: number;
  columnName: string;
  producerKind: string;
}): SeededEvidenceCell {
  return seedEvidence({
    pid,
    sheetId,
    mode: 'source',
    columnName,
    value: null,
    producerKind,
  });
}

function seedEvidence({
  pid,
  sheetId,
  mode,
  columnName,
  value,
  producerKind,
}: {
  pid: string;
  sheetId: number;
  mode: 'generated' | 'source';
  columnName: string;
  value: string | null;
  producerKind: string;
}): SeededEvidenceCell {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const payload = JSON.stringify({
    sheet_id: sheetId,
    mode,
    column_name: columnName,
    value,
    producer_kind: producerKind,
  });
  const script = String.raw`
import json
import sys
from pathlib import Path

from frisket.engine.store.evidence import record_evidence_link, record_source_artifact, record_source_span
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from tests.helpers import write_claimed_test_results

workspace, pid, payload_json = sys.argv[1], sys.argv[2], sys.argv[3]
payload = json.loads(payload_json)
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    run_store = RunResultStore(project)
    sheet_id = int(payload["sheet_id"])
    row = project.db.execute(
        "SELECT id FROM rows WHERE sheet_id=? ORDER BY position LIMIT 1",
        (sheet_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError("seed sheet has no rows")
    row_id = int(row["id"])
    columns = {
        row["name"]: int(row["id"])
        for row in project.db.execute(
            "SELECT id, name FROM columns WHERE sheet_id=?",
            (sheet_id,),
        ).fetchall()
    }
    producer_kind = str(payload["producer_kind"])
    if payload["mode"] == "generated":
        column_name = str(payload["column_name"])
        column_id = columns.get(column_name)
        if column_id is None:
            column_id = project.add_column(sheet_id, column_name, "text", ai_generated=True)
        op_id = project.append_op(
            producer_kind,
            {
                "schema_version": "frisket.action.v2",
                "kind": producer_kind,
                "params": {"output_column": column_name},
            },
            label=producer_kind,
        )
        run_id = run_store.start_run(
            op_id,
            sheet_id,
            producer_kind,
            model="fixture/model",
            params={"output_column": column_name},
            total_rows=1,
            row_ids=[row_id],
        )
        write_claimed_test_results(
            project,
            run_id,
            [
                {
                    "row_id": row_id,
                    "column_id": column_id,
                    "value": payload["value"],
                    "confidence": 0.91,
                    "justification": "Fixture evidence for the investigative action UI.",
                }
            ],
        )
        run_store.finish_run(run_id)
        run_store.point_column_at_run(op_id, column_id, run_id)
        _values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=[row_id])
        subject_kind = "cell_value"
        subject_ref = refs[row_id]
    else:
        column_name = str(payload["column_name"])
        column_id = columns[column_name]
        op_id = project.append_op(
            producer_kind,
            {"schema_version": "frisket.action.v2", "kind": producer_kind, "params": {}},
            label=producer_kind,
        )
        run_id = None
        _values, refs = project.get_values_with_refs(sheet_id, column_id, row_ids=[row_id])
        subject_kind = "cell"
        subject_ref = refs[row_id]

    blob_hash = project.add_blob(
        b"Fixture evidence for investigative action UI.",
        "investigative-evidence.txt",
        "text/plain",
    )
    artifact = record_source_artifact(
        project,
        artifact_kind="capture" if producer_kind in {"media.capture_url", "web.capture_page"} else "file",
        media_type="text/plain",
        blob_hash=blob_hash,
        title="Investigative evidence fixture",
        filename="investigative-evidence.txt",
        source_url="https://example.test/evidence",
    )
    span = record_source_span(
        project,
        artifact_id=artifact["id"],
        span_kind="text",
        quote="Fixture evidence",
        snippet="Fixture evidence for investigative action UI.",
    )
    link = record_evidence_link(
        project,
        subject_kind=subject_kind,
        subject_ref=subject_ref,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=column_id,
        run_id=run_id,
        op_id=op_id,
        receipt_id=f"receipt-{producer_kind}",
        link_role="primary_support",
        confidence=0.9,
        producer={"action_kind": producer_kind, "grounding_method": "fixture"},
        spans=[{"span_id": span["id"], "rank": 0, "span_role": "support"}],
    )
    project.db.commit()
    print(json.dumps({"rowId": row_id, "columnId": column_id, "stableId": link["stable_id"]}))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, payload], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededEvidenceCell;
}
