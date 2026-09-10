import { expect, test } from '@playwright/test';
import { createProject, openProject, runAndWait, sheetColumns, textPdf, uniqueName } from './helpers';
import { ensureShotsDir, makeShoot, shotsDir } from './screenshotHelpers';

// The settlement receipt's UI seat, driven against a LIVE backend.
//
// `attempt_settlement` produced receipts nothing rendered; this is the
// consumer. A local OCR run is routed (ocr.consumes_resolution is true) but
// operator-borne, so it settles to an ABSENCE — the exact case the panel must
// not turn into a $0.00 money row. What is asserted here is that honesty:
// the Charges section is present, says no charges, and shows no dollar figure
// anywhere.

const OUT_DIR = process.env.RECEIPTS_SHOT_DIR ?? shotsDir('run-receipts');
const shoot = makeShoot(OUT_DIR);

test('a free local OCR run shows an honest no-charges receipt panel', async ({ page }) => {
  test.setTimeout(180_000);
  ensureShotsDir(OUT_DIR);

  const pid = await createProject(page.request, uniqueName('run-receipts'));
  const importRes = await page.request.post(
    `/api/projects/${pid}/import/files?sheet_name=filings`,
    {
      multipart: {
        files: {
          name: 'board-minutes.pdf',
          mimeType: 'application/pdf',
          buffer: textPdf([['BOARD MINUTES', '', 'The committee approved the budget.']], {
            fontSize: 20,
            startX: 54,
            startY: 700,
            leading: 26,
          }),
        },
      },
    },
  );
  expect(importRes.ok()).toBeTruthy();

  const sheets = (await (await page.request.get(`/api/projects/${pid}/sheets`)).json()) as Array<{
    id: number;
    name: string;
  }>;
  const sheetId = sheets.find((sheet) => sheet.name === 'filings')!.id;
  const columns = await sheetColumns(page.request, pid, sheetId);
  const mediaColumn = columns.find((column) => ['file', 'image'].includes(column.type))!;

  const runId = await runAndWait(page.request, pid, {
    schema_version: 'frisket.action.v2',
    kind: 'media.ocr',
    capabilities: ['project:write', 'model:complete'],
    params: {
      sheet_id: sheetId,
      input_columns: [mediaColumn.name],
      engine: 'rapidocr',
      output_name: 'ocr_text',
      confirmed: true,
    },
  });

  // The reader itself: the run's attempt receipt exists and settled to an
  // absence rather than a charge.
  const receipts = (await (
    await page.request.get(`/api/projects/${pid}/actions/attempts?run_id=${runId}`)
  ).json()) as {
    total: number;
    attempts: Array<{ settlement: { charge_usd: string | null; pricing_key?: string | null } | null }>;
  };
  expect(receipts.total).toBeGreaterThan(0);
  expect(receipts.attempts[0].settlement?.pricing_key ?? null).toBeNull();

  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('bottom-dock-tab-jobs')).toHaveAttribute(
    'aria-selected',
    'true',
  );
  const jobRow = page.locator('[data-testid^="bottom-dock-job-"]').first();
  await expect(jobRow).toBeVisible();
  await jobRow.click();

  const receiptsSection = page.getByTestId('bottom-dock-job-receipts');
  await expect(receiptsSection).toBeVisible();
  await expect(page.getByTestId('bottom-dock-job-receipt-none')).toContainText('No charges');
  // The load-bearing negative: no fabricated money row for free work.
  expect(await receiptsSection.textContent()).not.toMatch(/\$/);
  await receiptsSection.scrollIntoViewIfNeeded();
  await shoot(page, 'run-detail-charges-free.png');
  await page
    .locator('aside.bottom-dock-detail')
    .screenshot({ path: `${OUT_DIR}/run-detail-charges-free-pane.png`, animations: 'disabled' });

  // Project level: the same component, unfiltered — where a compaction-
  // orphaned receipt (run_id null) is reachable at all.
  const project = await page.request.get(`/api/projects/${pid}/actions/attempts`);
  expect(project.ok()).toBeTruthy();
});
