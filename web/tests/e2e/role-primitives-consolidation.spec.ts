// Holistic re-examination of the role-attribute KEEP clusters. Four narrow
// extractions/fixes, each RED-first here:
//
//  1. <ResizeSeam> (src/components/ResizeSeam.tsx): the markup shell the four
//     useResizable seams (App.tsx work-split, DiscoverPanel, MediaCompareShell,
//     InspectDetailColumn) repeated verbatim now lives in one place, and gains
//     aria-valuenow/valuemin/valuemax none of the four exposed before. Covered
//     here: the Discover, Detail-column, and work-split seams (the fourth,
//     MediaCompareShell's peek-resize, is exercised for drag-resize mechanics
//     by workbench-ocr-compare.spec.ts, which still passes against the same
//     shared component — this spec adds the ARIA coverage none of the four
//     sites had).
//  2. useWindowedRowList (src/workbench/useWindowedRowList.ts): the
//     AnswersView/DocumentView "literal port pair" (AnswersView.tsx's own
//     comment) now share one scroll/window/measure/arrow-nav hook. Covered
//     here: ArrowUp/ArrowDown nav parity on both lists (same clamp-not-wrap
//     boundary behavior).
//  3. ModelPicker.renderOption (src/components/ModelPicker.tsx:253) gains
//     role="option" + aria-selected — both listbox panes (search results,
//     provider models) render through the same renderOption, so both were
//     malformed before this fix. Covered here via the search-results pane.
//  4. ActionPanel's two hand-rolled ".segmented source-mode-switch" toggles
//     (:2013 SourceInputControl, :4074 derive) now render through the
//     SegmentedToggle primitive the file already imports/uses elsewhere.
//     Covered here: the extract form's Column/Template toggle, and derive's
//     AI/existing-column toggle (including its disabled-option path).

import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import {
  addRow,
  createProject,
  importCsv,
  mockBasemapTiles,
  openAction,
  openCellDrawer,
  openProject,
  seedGeoSheet,
  sheetColumns,
  sheetData,
  textPdf,
  TINY_CSV,
  uniqueName,
  type WireColumn,
} from './helpers';

// ---------------------------------------------------------------------------
// 1. ResizeSeam — aria-valuenow/valuemin/valuemax on real DOM sites.

const CITY_CSV = 'city\nAlpha\nBravo\nCharlie\n';

async function openRowDetail(page: Page, columns: WireColumn[], rowIndex: number): Promise<void> {
  // 'city' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'city', rowIndex);
  await expect(page.getByTestId('row-drawer')).toBeVisible();
}

test('the Discover panel and Detail column resize seams expose live aria-valuenow/valuemin/valuemax (ResizeSeam)', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('resize-seam-aria'));
  const sheetId = await importCsv(request, pid, 'cities.csv', CITY_CSV);
  const columns = await sheetColumns(request, pid, sheetId);
  await openProject(page, pid, sheetId);

  // Discover panel: min 220 / max 520 (DiscoverPanel.tsx's DISCOVER_MIN/MAX_WIDTH).
  const discoverSeam = page.getByTestId('discover-seam');
  await expect(discoverSeam).toHaveAttribute('role', 'separator');
  await expect(discoverSeam).toHaveAttribute('aria-orientation', 'vertical');
  await expect(discoverSeam).toHaveAttribute('aria-valuemin', '220');
  await expect(discoverSeam).toHaveAttribute('aria-valuemax', '520');
  const discoverBefore = Number(await discoverSeam.getAttribute('aria-valuenow'));
  expect(discoverBefore).toBeGreaterThanOrEqual(220);
  expect(discoverBefore).toBeLessThanOrEqual(520);

  await discoverSeam.focus();
  for (let i = 0; i < 4; i++) await page.keyboard.press('ArrowLeft');
  await expect(discoverSeam).toHaveAttribute('aria-valuenow', String(discoverBefore + 96));
  await expect(page.getByTestId('discover-panel')).toHaveCSS(
    'width',
    `${discoverBefore + 96}px`,
  );

  // Detail column: min 260 / max 560 (InspectDetailColumn.tsx's
  // INSPECT_DETAIL_MIN/MAX_WIDTH).
  await openRowDetail(page, columns, 0);
  const detailSeam = page.getByTestId('inspect-detail-seam');
  await expect(detailSeam).toHaveAttribute('role', 'separator');
  await expect(detailSeam).toHaveAttribute('aria-valuemin', '260');
  await expect(detailSeam).toHaveAttribute('aria-valuemax', '560');
  const detailBefore = Number(await detailSeam.getAttribute('aria-valuenow'));

  await detailSeam.focus();
  for (let i = 0; i < 4; i++) await page.keyboard.press('ArrowLeft');
  await expect(detailSeam).toHaveAttribute('aria-valuenow', String(detailBefore + 96));
  await expect(page.getByTestId('row-drawer')).toHaveCSS('width', `${detailBefore + 96}px`);
});

test('the work-split seam (App.tsx WorkViewSplit) exposes aria-valuenow/valuemin/valuemax', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoSheet(page, { namePrefix: 'resize-seam-worksplit' });
  await mockBasemapTiles(page);
  await openProject(page, pid, sheetId);

  await page.getByTestId('view-switch-map').click();
  const seam = page.getByTestId('work-split-seam');
  await expect(seam).toBeVisible();
  await expect(seam).toHaveAttribute('aria-valuemin', '280');
  await expect(seam).toHaveAttribute('aria-valuemax', '820');
  const before = Number(await seam.getAttribute('aria-valuenow'));
  expect(before).toBeGreaterThanOrEqual(280);
  expect(before).toBeLessThanOrEqual(820);

  // handleEdge 'left' — ArrowLeft grows the companion (useResizable's
  // growKey/shrinkKey).
  await seam.focus();
  for (let i = 0; i < 4; i++) await page.keyboard.press('ArrowLeft');
  await expect(seam).toHaveAttribute('aria-valuenow', String(before + 96));
  await expect(page.getByTestId('work-split-companion')).toHaveCSS(
    'width',
    `${before + 96}px`,
  );
});

// ---------------------------------------------------------------------------
// 2. useWindowedRowList — AnswersView / DocumentView arrow-nav parity.

async function seedDocSheet(
  page: Page,
  totalRows = 3,
): Promise<{ pid: string; sheetId: number }> {
  const pid = await createProject(page.request, uniqueName('windowed-list-docview'));
  const importRes = await page.request.post(
    `/api/projects/${pid}/import/files?sheet_name=documents`,
    {
      multipart: {
        files: {
          name: 'first.pdf',
          mimeType: 'application/pdf',
          buffer: textPdf([['First', 'Page one.']]),
        },
      },
    },
  );
  expect(importRes.ok()).toBeTruthy();
  const sheets = (await (await page.request.get(`/api/projects/${pid}/sheets`)).json()) as Array<{
    id: number;
    name: string;
  }>;
  const sheet = sheets.find((candidate) => candidate.name === 'documents')!;
  const columns = await sheetColumns(page.request, pid, sheet.id);
  const mediaColumn = columns.find((column) => ['file', 'image', 'video'].includes(column.type))!;
  const firstPage = await sheetData(page.request, pid, sheet.id, 0, 5);
  const firstCell = firstPage.rows[0].cells[String(mediaColumn.id)] as Record<string, unknown>;
  for (let index = 1; index < totalRows; index += 1) {
    await addRow(page.request, pid, sheet.id, {
      [mediaColumn.name]: { ...firstCell, filename: `document-${index + 1}.pdf` },
    });
  }
  return { pid, sheetId: sheet.id };
}

test('DocumentView list arrow-nav clamps at the boundary (does not wrap)', async ({ page }) => {
  const { pid, sheetId } = await seedDocSheet(page);
  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-document').click();

  const items = page.getByTestId('document-list-item');
  await expect(items).toHaveCount(3);
  await expect(items.nth(0)).toHaveAttribute('data-active', 'true');

  const listBody = page.getByTestId('document-list-body');
  await listBody.focus();
  await page.keyboard.press('ArrowDown');
  await expect(items.nth(1)).toHaveAttribute('data-active', 'true');
  await page.keyboard.press('ArrowDown');
  await expect(items.nth(2)).toHaveAttribute('data-active', 'true');
  // Boundary: one more ArrowDown does not move past the last row.
  await page.keyboard.press('ArrowDown');
  await expect(items.nth(2)).toHaveAttribute('data-active', 'true');

  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('ArrowUp');
  await expect(items.nth(0)).toHaveAttribute('data-active', 'true');
  // Boundary: ArrowUp at the top stays put.
  await page.keyboard.press('ArrowUp');
  await expect(items.nth(0)).toHaveAttribute('data-active', 'true');
});

test('DocumentView list scroll follows arrow-key selection', async ({ page }) => {
  const { pid, sheetId } = await seedDocSheet(page, 24);
  await openProject(page, pid, sheetId);
  await page.getByTestId('view-switch-document').click();

  const listBody = page.getByTestId('document-list-body');
  await listBody.focus();
  for (let index = 0; index < 18; index += 1) {
    await page.keyboard.press('ArrowDown');
  }

  await expect.poll(() => listBody.evaluate((node) => node.scrollTop)).toBeGreaterThan(0);
  await expect.poll(() => listBody.evaluate((node) => {
    const active = node.querySelector<HTMLElement>('[data-active="true"]');
    if (!active) return false;
    return active.offsetTop >= node.scrollTop
      && active.offsetTop + active.offsetHeight <= node.scrollTop + node.clientHeight;
  })).toBe(true);

  for (let index = 0; index < 18; index += 1) {
    await page.keyboard.press('ArrowUp');
  }
  await expect.poll(() => listBody.evaluate((node) => node.scrollTop)).toBe(0);
  await expect(page.getByTestId('document-list-item').first()).toHaveAttribute('data-active', 'true');
});

interface SeededAnswersSheet {
  sheetId: number;
}

const REPO_ROOT =
  path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();

function seedAnswersSheet(pid: string): SeededAnswersSheet {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import json
import sys
from pathlib import Path

from frisket.engine.store.evidence import record_evidence_link, record_source_artifact, record_source_span
from frisket.engine.store import Project
from frisket.engine.store.runs import RunResultStore
from tests.helpers import write_claimed_test_results

workspace, pid = sys.argv[1], sys.argv[2]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    run_store = RunResultStore(project)
    sheet_id = project.add_sheet("Docs")
    name_col = project.add_column(sheet_id, "Name", "text")
    source_col = project.add_column(sheet_id, "Source", "file")
    summary_col = project.add_column(sheet_id, "Summary", "text", ai_generated=True)
    names = ["Acme Corp", "Beta LLC", "Gamma Inc"]
    row_ids = project.add_rows(
        sheet_id,
        [{"Name": n, "Source": f"{n}.pdf"} for n in names],
        {"Name": name_col, "Source": source_col},
    )

    op_id = project.append_op(
        "map.extract",
        {"schema_version": "frisket.action.v2", "kind": "map.extract", "params": {"output_column": "Summary"}},
        label="extract Summary",
    )
    run_id = run_store.start_run(
        op_id, sheet_id, "map.extract", model="provider/model",
        params={"output_column": "Summary"}, total_rows=len(row_ids), row_ids=row_ids,
    )
    summary_values = ["Revenue grew.", "Headcount doubled.", "Expanded markets."]
    write_claimed_test_results(project, run_id, [
        {"row_id": rid, "column_id": summary_col, "value": v, "confidence": 0.9}
        for rid, v in zip(row_ids, summary_values)
    ])
    run_store.finish_run(run_id)
    run_store.point_column_at_run(op_id, summary_col, run_id)
    _values, refs = project.get_values_with_refs(sheet_id, summary_col, row_ids=row_ids)
    artifact = record_source_artifact(
        project, artifact_kind="file", media_type="application/pdf", title="Contract", filename="doc.pdf",
    )
    for rid, v in zip(row_ids, summary_values):
        span = record_source_span(
            project, artifact_id=artifact["id"], span_kind="region", page_start=1, page_end=1, quote=v, snippet=v,
        )
        record_evidence_link(
            project, subject_kind="cell_value", subject_ref=refs[rid],
            sheet_id=sheet_id, row_id=rid, column_id=summary_col, run_id=run_id, op_id=op_id,
            link_role="primary_support", confidence=0.9, spans=[{"span_id": span["id"], "rank": 0}],
        )

    project.db.commit()
    print(json.dumps({"sheetId": sheet_id}))
finally:
    project.close()
`;
  const output = execFileSync('uv', ['run', 'python', '-c', script, workspace, pid], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
  });
  return JSON.parse(output) as SeededAnswersSheet;
}

test('AnswersView row list arrow-nav clamps at the boundary — same shape as DocumentView (useWindowedRowList)', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('windowed-list-answers'));
  const seeded = seedAnswersSheet(pid);
  await openProject(page, pid, seeded.sheetId);
  await page.getByTestId('view-switch-answers').click();
  await expect(page.getByTestId('grounded-answers-view')).toBeVisible();

  const items = page.getByTestId('answers-row-item');
  await expect(items).toHaveCount(3);
  await expect(items.nth(0)).toHaveAttribute('data-active', 'true');

  const listBody = page.getByTestId('answers-row-list-body');
  await listBody.focus();
  await page.keyboard.press('ArrowDown');
  await expect(items.nth(1)).toHaveAttribute('data-active', 'true');
  await page.keyboard.press('ArrowDown');
  await expect(items.nth(2)).toHaveAttribute('data-active', 'true');
  // Boundary: same clamp-not-wrap behavior as DocumentView above.
  await page.keyboard.press('ArrowDown');
  await expect(items.nth(2)).toHaveAttribute('data-active', 'true');

  await page.keyboard.press('ArrowUp');
  await page.keyboard.press('ArrowUp');
  await expect(items.nth(0)).toHaveAttribute('data-active', 'true');
  await page.keyboard.press('ArrowUp');
  await expect(items.nth(0)).toHaveAttribute('data-active', 'true');
});

// ---------------------------------------------------------------------------
// 3. ModelPicker.renderOption — role="option" + aria-selected.

async function openClassifyForm(page: Page, pid: string): Promise<void> {
  await importCsv(page.request, pid, 'snippets.csv', TINY_CSV);
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  await openAction(page, 'map.classify');
  await expect(page.getByTestId('action-form')).toBeVisible();
}

test('ModelPicker option rows carry role="option" + aria-selected (both search and provider panes render via the same renderOption)', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('model-picker-option-aria'));
  await openClassifyForm(page, pid);

  await page.getByTestId('model-picker-button').click();
  const menu = page.getByTestId('model-picker-menu');
  await expect(menu).toBeVisible();

  // Search mode (.model-picker-search-results, ModelPicker.tsx:330 — a
  // className, not a testid) — 'a' matches broadly regardless of the live vs.
  // fallback catalog's exact provider/model names.
  await page.getByTestId('model-picker-search').fill('a');
  const results = menu.locator('.model-picker-search-results');
  await expect(results).toBeVisible();
  const options = results.locator('[data-testid^="model-option-"]');
  await expect(options.first()).toBeVisible();
  const optionCount = await options.count();
  expect(optionCount).toBeGreaterThan(0);
  for (let i = 0; i < optionCount; i++) {
    await expect(options.nth(i)).toHaveAttribute('role', 'option');
    // Every option has an aria-selected value (true or false) — never absent.
    await expect(options.nth(i)).toHaveAttribute('aria-selected', /true|false/);
  }
  // Exactly one option reflects the current selection.
  const selectedCount = await results.locator('[data-testid^="model-option-"][aria-selected="true"]').count();
  expect(selectedCount).toBe(1);

  // Selecting a DIFFERENT (not-yet-selected) option moves aria-selected="true"
  // onto it — the option's own state, not just the trigger's summary text.
  const unselected = results.locator('[data-testid^="model-option-"][aria-selected="false"]').first();
  const unselectedTestId = await unselected.getAttribute('data-testid');
  await unselected.click();
  await expect(menu).toBeHidden();

  await page.getByTestId('model-picker-button').click();
  await expect(menu).toBeVisible();
  await page.getByTestId('model-picker-search').fill('a');
  const reopened = page.getByTestId(unselectedTestId!);
  await expect(reopened).toHaveAttribute('role', 'option');
  await expect(reopened).toHaveAttribute('aria-selected', 'true');
});

// ---------------------------------------------------------------------------
// 4. ActionPanel's two source-mode switches adopt SegmentedToggle.

test('the extract form Column/Template toggle behaves as a SegmentedToggle (aria-pressed parity, panel swap)', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('source-mode-segmented-extract'));
  await importCsv(page.request, pid, 'meetings.csv', 'title,notes\n"Budget hearing","Vendor bids."\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  await openAction(page, 'map.extract');

  const columnBtn = page.getByTestId('text-source-mode-column');
  const templateBtn = page.getByTestId('text-source-mode-template');
  await expect(columnBtn).toHaveAttribute('aria-pressed', 'true');
  await expect(templateBtn).toHaveAttribute('aria-pressed', 'false');
  await expect(page.getByTestId('text-source-column-select')).toBeVisible();

  await templateBtn.click();
  await expect(templateBtn).toHaveAttribute('aria-pressed', 'true');
  await expect(columnBtn).toHaveAttribute('aria-pressed', 'false');
  await expect(page.getByTestId('text-source-template-input')).toBeVisible();
  await expect(page.getByTestId('text-source-column-select')).toHaveCount(0);

  await columnBtn.click();
  await expect(columnBtn).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByTestId('text-source-template-input')).toHaveCount(0);
  await expect(page.getByTestId('text-source-column-select')).toBeVisible();
});

test('the derive AI/existing-column toggle behaves as a SegmentedToggle, including its disabled option (no JSON list columns)', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('source-mode-segmented-derive'));
  await importCsv(page.request, pid, 'episodes.csv', 'title\n"Episode 1"\n');
  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  await openAction(page, 'derive.table_from_list');

  const aiBtn = page.getByTestId('derive-source-mode-ai');
  const columnBtn = page.getByTestId('derive-source-mode-column');
  await expect(aiBtn).toHaveAttribute('aria-pressed', 'true');
  await expect(columnBtn).toHaveAttribute('aria-pressed', 'false');

  // No JSON list column on this sheet — the option renders disabled, with the
  // SAME reason text the hand-rolled toggle used as its title.
  await expect(columnBtn).toBeDisabled();
  await expect(columnBtn).toHaveAttribute('title', 'No JSON list columns on this sheet');
  await columnBtn.click({ force: true });
  await expect(aiBtn).toHaveAttribute('aria-pressed', 'true');
});
