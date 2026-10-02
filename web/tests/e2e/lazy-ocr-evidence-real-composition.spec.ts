import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject, dblclickCell, openProject, sheetColumns, textPdf, uniqueName } from './helpers';

const REPO_ROOT = path.basename(process.cwd()) === 'web'
  ? path.resolve(process.cwd(), '..') : process.cwd();

function seedLazyEvidence(pid: string, sheetId: number): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import sys
from pathlib import Path
from frisket.engine.store import Project
from frisket.engine.store.evidence import record_evidence_link, record_source_artifact, record_source_span
from frisket.engine.store.runs import RunResultStore
from tests.helpers import write_claimed_test_results

workspace, pid, sheet_id = sys.argv[1], sys.argv[2], int(sys.argv[3])
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    row_id = int(project.db.execute("SELECT id FROM rows WHERE sheet_id=? ORDER BY id LIMIT 1", (sheet_id,)).fetchone()["id"])
    source_column_id = int(project.db.execute("SELECT id FROM columns WHERE sheet_id=? AND type='file'", (sheet_id,)).fetchone()["id"])
    blob_hash = project.db.execute("SELECT hash FROM blobs ORDER BY rowid LIMIT 1").fetchone()["hash"]
    text_column_id = project.add_column(sheet_id, "Text", "text", ai_generated=True)
    op_id = project.append_op("media.ocr", {"schema_version": "frisket.action.v2", "kind": "media.ocr", "params": {"output_name": "Text"}}, label="fixture OCR")
    runs = RunResultStore(project)
    run_id = runs.start_run(op_id, sheet_id, "media.ocr", model="local/fixture", params={"output_name": "Text"}, total_rows=1, row_ids=[row_id])
    write_claimed_test_results(project, run_id, [{"row_id": row_id, "column_id": text_column_id, "value": "page one page two total 42", "confidence": 0.9, "justification": "fixture"}])
    runs.finish_run(run_id)
    runs.point_column_at_run(op_id, text_column_id, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, text_column_id, row_ids=[row_id])
    artifact = record_source_artifact(
        project, artifact_kind="file", media_type="application/pdf", blob_hash=blob_hash,
        filename="source.pdf", page_count=2, source_sheet_id=sheet_id,
        source_row_id=row_id, source_column_id=source_column_id,
        metadata={"engine": "fixture", "output_name": "Text", "page_images": {
            "1": {"mime": "application/pdf", "source_width": 1224, "source_height": 1584},
            "2": {"mime": "application/pdf", "source_width": 1224, "source_height": 1584},
        }},
    )
    spans = [record_source_span(project, artifact_id=artifact["id"], span_kind="page_range", page_start=page, page_end=page, snippet=f"page {page}", metadata={"engine": "fixture"}) for page in (1, 2)]
    record_evidence_link(
        project, subject_kind="cell", subject_ref=refs[row_id],
        spans=[{"span_id": span["id"], "rank": index} for index, span in enumerate(spans)],
        sheet_id=sheet_id, row_id=row_id, column_id=text_column_id, run_id=run_id,
        op_id=op_id, receipt_id="receipt-lazy-pdf", link_role="media_ocr_grounding",
        producer={"action_kind": "media.ocr", "engine": "fixture"},
    )
    project.db.commit()
finally:
    project.close()
`;
  execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, String(sheetId)], {
    cwd: REPO_ROOT, encoding: 'utf8', timeout: 120_000,
  });
}

test('retained PDF evidence renders its two pages lazily through the authenticated API', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('lazy-ocr-evidence'));
  const imported = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=documents`, {
    multipart: { files: { name: 'source.pdf', mimeType: 'application/pdf', buffer: textPdf([
      ['Page one'], ['Page two'],
    ]) } },
  });
  expect(imported.ok()).toBeTruthy();
  const sheets = await (await page.request.get(`/api/projects/${pid}/sheets`)).json();
  const sheet = sheets.find((candidate: { name: string }) => candidate.name === 'documents');
  seedLazyEvidence(pid, sheet.id);
  const columns = await sheetColumns(page.request, pid, sheet.id);
  const pageResponses: string[] = [];
  page.on('response', (response) => {
    if (response.url().includes('/pages/') && response.url().endsWith('/image') && response.ok()) {
      pageResponses.push(response.url());
    }
  });

  await openProject(page, pid, sheet.id);
  await dblclickCell(page, columns, 'Text', 0);
  await page.getByTestId('cell-evidence-open-Text').click();
  const viewer = page.getByTestId('evidence-viewer');
  await expect(viewer).toBeVisible();
  const pages = viewer.getByTestId('evidence-page');
  await expect(pages).toHaveCount(2);
  const page1 = pages.filter({ hasText: 'Page 1' });
  await page1.evaluate((node) => node.scrollIntoView());
  const image1 = page1.getByTestId('evidence-page-image');
  await expect(image1).toBeVisible();
  await expect.poll(() => image1.evaluate((node) => ({
    loaded: (node as HTMLImageElement).complete
      && (node as HTMLImageElement).naturalWidth > 0,
    source: (node as HTMLImageElement).src,
  }))).toEqual(expect.objectContaining({
    loaded: true, source: expect.stringContaining('/pages/1/image'),
  }));
  await expect.poll(() => pageResponses.some((url) => url.includes('/pages/1/image'))).toBe(true);

  const page2 = pages.filter({ hasText: 'Page 2' });
  await page2.evaluate((node) => node.scrollIntoView());
  const image2 = page2.getByTestId('evidence-page-image');
  await expect(image2).toBeVisible();
  await expect.poll(() => image2.evaluate((node) => ({
    loaded: (node as HTMLImageElement).complete
      && (node as HTMLImageElement).naturalWidth > 0,
    source: (node as HTMLImageElement).src,
  }))).toEqual(expect.objectContaining({
    loaded: true, source: expect.stringContaining('/pages/2/image'),
  }));
  await expect.poll(() => pageResponses.some((url) => url.includes('/pages/2/image'))).toBe(true);
});
