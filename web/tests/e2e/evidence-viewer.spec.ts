import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import {
  createProject,
  dblclickCell,
  editCells,
  openProject,
  sheetColumns,
  uniqueName,
} from './helpers';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

type SeededEvidence = {
  sheetId: number;
  rowId: number;
  sourceColumnId: number;
  outputColumnId: number;
  activeLinkStableId: string;
  sourceLinkStableId: string;
};

function seedEvidenceProject(pid: string): SeededEvidence {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import base64
import json
import sys
from pathlib import Path

from frisket.engine.store.evidence import (
    _text_hash,
    record_evidence_link,
    record_source_artifact,
    record_source_span,
    record_text_surface,
)
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from tests.helpers import write_claimed_test_results

workspace, pid = sys.argv[1], sys.argv[2]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    run_store = RunResultStore(project)
    sheet_id = project.add_sheet("Documents")
    source_column_id = project.add_column(sheet_id, "Source", "file")
    output_column_id = project.add_column(sheet_id, "Contract value", "text", ai_generated=True)
    row_id = project.add_rows(sheet_id, [{"Source": "contract.pdf"}], {"Source": source_column_id})[0]
    op_id = project.append_op(
        "map.extract",
        {
            "schema_version": "frisket.action.v2",
            "kind": "map.extract",
            "params": {"output_column": "Contract value"},
        },
        label="extract contract value",
    )
    run_id = run_store.start_run(
        op_id,
        sheet_id,
        "map.extract",
        model="provider/model",
        params={"output_column": "Contract value"},
        total_rows=1,
        row_ids=[row_id],
    )
    write_claimed_test_results(
        project,
        run_id,
        [
            {
                "row_id": row_id,
                "column_id": output_column_id,
                "value": "$1,250,000",
                "confidence": 0.92,
                "justification": "The amount appears in the contract page image.",
            }
        ],
    )
    run_store.finish_run(run_id)
    run_store.point_column_at_run(op_id, output_column_id, run_id)
    _source_values, source_refs = project.get_values_with_refs(sheet_id, source_column_id, row_ids=[row_id])
    _values, refs = project.get_values_with_refs(sheet_id, output_column_id, row_ids=[row_id])
    source_ref = source_refs[row_id]
    current_ref = refs[row_id]

    pdf_hash = project.add_blob(b"%PDF-1.4 public contract", "contract.pdf", "application/pdf")
    # Valid 1x1 PNG; metadata supplies source page dimensions for the evidence contract.
    page_image_hash = project.add_blob(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="),
        "contract-p3.png",
        "image/png",
    )
    html_hash = project.add_blob(
        b"<html><body><p id='award'>Award value was $1,250,000.</p></body></html>",
        "minutes.html",
        "text/html",
        source_url="https://example.test/minutes",
    )
    audio_hash = project.add_blob(b"fixture audio bytes", "hearing.mp3", "audio/mpeg")
    video_hash = project.add_blob(b"fixture video bytes", "hearing.mp4", "video/mp4")

    page_text = "The total contract value is $1,250,000 payable in March."
    pdf_artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="application/pdf",
        blob_hash=pdf_hash,
        title="Contract",
        filename="contract.pdf",
        page_count=9,
        metadata={
            "page_images": {
                "3": {"blob_hash": page_image_hash, "width": 400, "height": 520}
            },
            "text_pages": {
                "3": page_text
            },
        },
    )
    html_artifact = record_source_artifact(
        project,
        artifact_kind="capture",
        media_type="text/html",
        blob_hash=html_hash,
        source_url="https://example.test/minutes",
        canonical_url="https://example.test/minutes",
        title="Board minutes",
        filename="minutes.html",
    )
    audio_artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="audio/mpeg",
        blob_hash=audio_hash,
        filename="hearing.mp3",
        duration_ms=180000,
    )
    video_artifact = record_source_artifact(
        project,
        artifact_kind="file",
        media_type="video/mp4",
        blob_hash=video_hash,
        filename="hearing.mp4",
        duration_ms=300000,
    )
    table_artifact = record_source_artifact(
        project,
        artifact_kind="external_record",
        media_type="application/vnd.frisket.table+json",
        title="Extracted bid table",
        external_ref={"system": "fixture", "record_id": "bid-table-7"},
    )
    page_text_hash = _text_hash(page_text)
    page_text_surface = record_text_surface(
        project,
        surface_kind="composite",
        content_hash=page_text_hash,
        offset_unit="unicode_codepoint",
        surface_ref={
            "identity": {
                "artifact_id": pdf_artifact["id"],
                "page": 3,
                "source": "metadata.text_pages",
            }
        },
    )
    spans = [
        record_source_span(project, artifact_id=pdf_artifact["id"], span_kind="whole", snippet="Contract document"),
        record_source_span(
            project,
            artifact_id=pdf_artifact["id"],
            span_kind="region",
            page_start=3,
            page_end=3,
            bbox=[{"space": "page_normalized", "x0": 0.20, "y0": 0.26, "x1": 0.74, "y1": 0.42}],
            quote="$1,250,000",
            metadata={"raw": {"provider_space": "pixels", "x0": 80, "y0": 135, "x1": 296, "y1": 218}},
        ),
        record_source_span(
            project,
            artifact_id=pdf_artifact["id"],
            span_kind="text",
            char_start=28,
            char_end=38,
            quote="$1,250,000",
            snippet="total contract value is $1,250,000",
            text_layer_hash=page_text_hash,
            text_surface_id=page_text_surface["id"],
        ),
        record_source_span(
            project,
            artifact_id=html_artifact["id"],
            span_kind="html",
            selector={"css": "#award", "quote_context": "p#award"},
            quote="Award value was $1,250,000.",
        ),
        record_source_span(
            project,
            artifact_id=audio_artifact["id"],
            span_kind="temporal",
            start_ms=81234,
            end_ms=93410,
            quote="the contract value was one point two five million",
        ),
        record_source_span(
            project,
            artifact_id=video_artifact["id"],
            span_kind="temporal",
            start_ms=120000,
            end_ms=132000,
            bbox=[{"space": "frame_normalized", "x0": 0.12, "y0": 0.22, "x1": 0.48, "y1": 0.66}],
            selector={"representative_frame_ms": 124500},
            quote="slide showing $1.25M",
        ),
        record_source_span(
            project,
            artifact_id=table_artifact["id"],
            span_kind="table",
            selector={"table_index": 0, "row_index": 2, "column_name": "amount", "cell": "C3"},
            snippet="amount=$1,250,000",
        ),
    ]
    link = record_evidence_link(
        project,
        subject_kind="cell_value",
        subject_ref=current_ref,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=output_column_id,
        run_id=run_id,
        op_id=op_id,
        receipt_id="receipt-map-extract",
        link_role="primary_support",
        confidence=0.91,
        producer={"action_kind": "map.extract", "model": "provider/model", "grounding_method": "model_bbox"},
        metadata={"warnings": ["cross_artifact_support"]},
        spans=[
            {
                "span_id": span["id"],
                "rank": index,
                "span_role": "support",
                "required": index == 1,
            }
            for index, span in enumerate(spans)
        ],
    )
    source_link = record_evidence_link(
        project,
        subject_kind="cell",
        subject_ref=source_ref,
        sheet_id=sheet_id,
        row_id=row_id,
        column_id=source_column_id,
        run_id=run_id,
        op_id=op_id,
        receipt_id="receipt-source",
        link_role="source_document",
        confidence=1.0,
        producer={"action_kind": "import.file"},
        spans=[{"span_id": spans[0]["id"], "rank": 0, "span_role": "source"}],
    )
    project.db.commit()
    print(json.dumps({
        "sheetId": sheet_id,
        "rowId": row_id,
        "sourceColumnId": source_column_id,
        "outputColumnId": output_column_id,
        "activeLinkStableId": link["stable_id"],
        "sourceLinkStableId": source_link["stable_id"],
    }))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededEvidence;
}

test('row drawer opens the shared evidence viewer and hides stale support by default', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-evidence-viewer'));
  const seeded = seedEvidenceProject(pid);
  const columns = await sheetColumns(request, pid, seeded.sheetId);

  const activeEvidenceResponse = await request.get(
    `/api/projects/${pid}/cells/${seeded.rowId}/${seeded.outputColumnId}/evidence`,
  );
  expect(activeEvidenceResponse.ok()).toBeTruthy();
  expect((await activeEvidenceResponse.json()).links).toContainEqual(
    expect.objectContaining({
      stable_id: seeded.activeLinkStableId,
      status: 'active',
    }),
  );

  await openProject(page, pid, seeded.sheetId);
  await dblclickCell(page, columns, 'Contract value', 0);
  const rowDrawer = page.getByTestId('row-drawer');
  await expect(rowDrawer).toBeVisible();
  const sourceEvidenceSection = rowDrawer
    .getByTestId('workbench-contribution-frisket-core-row-inspector-section-evidence')
    .filter({ has: page.getByTestId('cell-evidence-active-Source') });
  await expect(sourceEvidenceSection).toBeVisible();
  await expect(sourceEvidenceSection).toHaveAttribute(
    'data-contribution-id',
    'frisket.core.row_inspector.section.evidence',
  );
  await expect(sourceEvidenceSection).toHaveAttribute('data-schema-version', 'frisket.row_inspector.section.v1');
  await expect(sourceEvidenceSection).toHaveAttribute('data-host', 'rightInspector');
  await expect(sourceEvidenceSection).toHaveAttribute('data-mode', 'section');
  await expect(sourceEvidenceSection).toHaveAttribute('data-tab-host', 'rowDetail');
  await expect(sourceEvidenceSection).toHaveAttribute('data-tab-mode', 'tab');
  await expect(sourceEvidenceSection).toHaveAttribute('data-tab-placement-id', 'row-evidence-tab');
  await expect(sourceEvidenceSection).toHaveAttribute(
    'data-runtime-component-key',
    'core.rowInspector.CellEvidenceBlock',
  );
  await expect(sourceEvidenceSection).toHaveAttribute('data-required-capabilities', /evidence\.listCell/);
  await expect(sourceEvidenceSection).toHaveAttribute('data-required-capabilities', /evidence\.open/);
  await expect(sourceEvidenceSection.getByTestId('cell-evidence-active-Source')).toBeVisible();
  await rowDrawer.getByTestId('cell-evidence-open-Source').click();
  const sourceEvidenceContribution = page.getByTestId('workbench-contribution-frisket-core-view-evidence');
  await expect(sourceEvidenceContribution).toBeVisible();
  await expect(sourceEvidenceContribution).toHaveAttribute('data-contribution-id', 'frisket.core.view.evidence');
  await expect(sourceEvidenceContribution).toHaveAttribute('data-host', 'modalOrPeek');
  await expect(sourceEvidenceContribution).toHaveAttribute('data-mode', 'peek');
  await expect(sourceEvidenceContribution).toHaveAttribute('data-runtime-component-key', 'core.views.EvidenceViewer');
  await expect(sourceEvidenceContribution).toHaveAttribute('data-required-capabilities', /evidence\.resolve/);
  await expect(sourceEvidenceContribution).toHaveAttribute('data-required-capabilities', /sourceArtifact\.resolve/);
  await expect(sourceEvidenceContribution).toHaveAttribute('data-required-capabilities', /grid\.companion\.open/);
  await expect(page.getByTestId('evidence-viewer').getByTestId('evidence-export-ref')).toContainText(
    seeded.sourceLinkStableId,
  );
  await page.getByLabel('Close evidence viewer').click();

  const generatedEvidenceSection = rowDrawer
    .getByTestId('workbench-contribution-frisket-core-row-inspector-section-evidence')
    .filter({ has: page.getByTestId('cell-evidence-active-Contract value') });
  await expect(generatedEvidenceSection).toBeVisible();
  await expect(generatedEvidenceSection).toHaveAttribute('data-host', 'rightInspector');
  await expect(generatedEvidenceSection).toHaveAttribute('data-mode', 'section');
  await expect(generatedEvidenceSection).toHaveAttribute('data-tab-host', 'rowDetail');
  await expect(generatedEvidenceSection).toHaveAttribute('data-tab-mode', 'tab');
  await expect(generatedEvidenceSection.getByTestId('cell-evidence-active-Contract value')).toBeVisible();
  await rowDrawer.getByTestId('cell-evidence-open-Contract value').click();

  const viewer = page.getByTestId('evidence-viewer');
  await expect(viewer).toBeVisible();
  await expect(viewer.getByTestId('evidence-link-status')).toContainText('active');
  await expect(viewer.getByTestId('evidence-export-ref')).toContainText('evidence_link:');
  await expect(viewer.getByTestId('evidence-viewer-warnings')).toContainText('cross_artifact_support');
  await expect(viewer.getByTestId('evidence-page-image')).toBeVisible();
  await expect(viewer.getByTestId('evidence-region-highlight')).toBeVisible();
  await expect(viewer.getByTestId('evidence-page-text')).toContainText('$1,250,000');
  await expect(viewer.getByTestId('evidence-snippet').filter({ hasText: '$1,250,000' }).first()).toBeVisible();
  await expect(viewer.getByTestId('evidence-span-text')).toBeVisible();
  await expect(viewer.getByTestId('evidence-span-html')).toContainText('Award value');
  await expect(viewer.getByTestId('evidence-span-table')).toContainText('amount=$1,250,000');
  await expect(viewer.getByTestId('evidence-span-temporal').first()).toContainText('the contract value');
  await expect(viewer.getByTestId('evidence-temporal-clip').first()).toContainText('1:21-1:33');
  await expect(viewer.getByTestId('evidence-copy-ref')).toBeVisible();

  const imageLoaded = await viewer.getByTestId('evidence-page-image').evaluate((node) => {
    const img = node as HTMLImageElement;
    return img.complete && img.naturalWidth > 0 && img.naturalHeight > 0;
  });
  expect(imageLoaded).toBeTruthy();

  await page.getByLabel('Open evidence beside grid').click();
  const split = page.getByTestId('workbench-mainView-split');
  await expect(split).toBeVisible();
  await expect(page.getByTestId('workbench-contribution-frisket-core-view-grid')).toBeVisible();
  const companionEvidenceContribution = page.getByTestId('workbench-contribution-frisket-core-view-evidence');
  await expect(companionEvidenceContribution).toBeVisible();
  await expect(companionEvidenceContribution).toHaveAttribute('data-host', 'mainView');
  await expect(companionEvidenceContribution).toHaveAttribute('data-mode', 'pane');
  await expect(companionEvidenceContribution.getByTestId('evidence-export-ref')).toContainText(
    seeded.activeLinkStableId,
  );

  await page.getByLabel('Close evidence viewer').click();
  await expect(viewer).toBeHidden();

  await editCells(request, pid, [
    {
      rowId: seeded.rowId,
      columnId: seeded.outputColumnId,
      value: 'manually corrected value',
    },
  ]);
  const staleDefault = await request.get(
    `/api/projects/${pid}/cells/${seeded.rowId}/${seeded.outputColumnId}/evidence`,
  );
  expect(staleDefault.ok()).toBeTruthy();
  const staleDefaultPayload = await staleDefault.json();
  expect(staleDefaultPayload.links).toEqual([]);
  expect(staleDefaultPayload.stale_count).toBe(1);

  await openProject(page, pid, seeded.sheetId);
  await dblclickCell(page, columns, 'Contract value', 0);
  const staleDrawer = page.getByTestId('row-drawer');
  await expect(staleDrawer.getByTestId('cell-evidence-active-Contract value')).toHaveCount(0);
  const staleEvidenceSection = staleDrawer
    .getByTestId('workbench-contribution-frisket-core-row-inspector-section-evidence')
    .filter({ has: page.getByTestId('cell-evidence-stale-toggle-Contract value') });
  await expect(staleEvidenceSection).toBeVisible();
  await expect(staleEvidenceSection).toHaveAttribute('data-host', 'rightInspector');
  await expect(staleEvidenceSection).toHaveAttribute('data-mode', 'section');
  await expect(staleEvidenceSection).toHaveAttribute('data-tab-host', 'rowDetail');
  await expect(staleEvidenceSection).toHaveAttribute('data-tab-mode', 'tab');
  await expect(staleEvidenceSection.getByTestId('cell-evidence-stale-toggle-Contract value')).toBeVisible();
  await staleDrawer.getByTestId('cell-evidence-stale-toggle-Contract value').click();
  await expect(staleDrawer.getByTestId('cell-evidence-stale-Contract value')).toBeVisible();
  await staleDrawer.getByTestId('cell-evidence-open-stale-Contract value').click();
  await expect(page.getByTestId('evidence-viewer').getByTestId('evidence-link-status')).toContainText('stale');
  await expect(page.getByTestId('evidence-viewer').getByTestId('evidence-stale-reason')).toContainText('manual_cell_edit');
});
