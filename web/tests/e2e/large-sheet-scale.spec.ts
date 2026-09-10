import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';

import { expect, test } from '@playwright/test';

import {
  createProject,
  importCsv,
  openFriendlyFilterSidebar,
  openAdvancedSortPanel,
  openHistory,
  openToolbarOverflow,
  runAndWait,
  sheetColumns,
  sheetData,
  uniqueName,
} from './helpers';

const REPO_ROOT =
  path.basename(process.cwd()) === 'web'
    ? path.resolve(process.cwd(), '..')
    : process.cwd();

function largeCsv(rowCount: number): string {
  const lines = ['title,status,rank'];
  for (let i = 0; i < rowCount; i += 1) {
    const status = i % 2 === 0 ? 'open' : 'closed';
    lines.push(`${JSON.stringify(`Scale row ${i}`)},${status},${i}`);
  }
  return `${lines.join('\n')}\n`;
}

function seedExtraHistory(pid: string, count: number): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = `
import sys
from pathlib import Path
from frisket.engine.store import Project

workspace, pid, count_raw = sys.argv[1:]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    for index in range(int(count_raw)):
        project.append_op("edit", {"scale_index": index}, label=f"scale edit {index + 1}")
finally:
    project.close()
`;
  execFileSync('uv', ['run', 'python', '-c', script, workspace, pid, String(count)], {
    cwd: REPO_ROOT,
    stdio: 'pipe',
    timeout: 60_000,
  });
}

function pathMatches(url: URL, pathname: string): boolean {
  return url.pathname === pathname;
}

test('large sheet product paths use bounded browser traffic', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-large-sheet-scale'));
  const sheetId = await importCsv(page.request, pid, 'large.csv', largeCsv(1600));
  const columns = await sheetColumns(page.request, pid, sheetId);
  const firstRows = await sheetData(page.request, pid, sheetId, 0, 5);
  await runAndWait(page.request, pid, {
    action_id: 'map.template',
    scope: {
      kind: 'sheet_rows',
      sheet_id: sheetId,
      row_ids: firstRows.rows.map((row) => row.id),
    },
    params: { template: { text: '{{title}}' } },
    output_names: { rendered: 'tag' },
    idempotency_key: `large-sheet-template-${pid}`,
  });
  seedExtraHistory(pid, 65);

  const far = await sheetData(page.request, pid, sheetId, 1500, 1);
  const farRow = far.rows[0];
  expect(farRow.cells).toBeTruthy();

  const dataRequests: URL[] = [];
  const locateRequests: URL[] = [];
  const historyRequests: URL[] = [];
  const provenanceRequests: URL[] = [];
  const reviewCountRequests: URL[] = [];
  const reviewQueueRequests: URL[] = [];
  const traceRequests: URL[] = [];
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const httpErrors: Array<{ status: number; pathname: string }> = [];

  page.on('console', (message) => {
    const text = message.text();
    if (
      message.type() === 'error' &&
      text !== 'Failed to load resource: the server responded with a status of 404 (Not Found)'
    ) {
      consoleErrors.push(text);
    }
  });
  page.on('pageerror', (error) => pageErrors.push(error.message));
  page.on('response', (response) => {
    if (response.status() < 400) return;
    const url = new URL(response.url());
    httpErrors.push({ status: response.status(), pathname: url.pathname });
  });

  await page.route(`**/api/projects/${pid}/**`, async (route) => {
    const url = new URL(route.request().url());
    if (pathMatches(url, `/api/projects/${pid}/sheets/${sheetId}/data`)) {
      dataRequests.push(url);
    }
    if (pathMatches(url, `/api/projects/${pid}/sheets/${sheetId}/rows/${farRow.id}/locate`)) {
      locateRequests.push(url);
    }
    if (pathMatches(url, `/api/projects/${pid}/history`)) {
      historyRequests.push(url);
    }
    if (pathMatches(url, `/api/projects/${pid}/provenance`)) {
      provenanceRequests.push(url);
    }
    if (pathMatches(url, `/api/projects/${pid}/review/count`)) {
      reviewCountRequests.push(url);
    }
    if (pathMatches(url, `/api/projects/${pid}/review/queue`)) {
      reviewQueueRequests.push(url);
      await route.fulfill({
        status: 599,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'large-sheet scale proof should use /review/count' }),
      });
      return;
    }
    if (url.pathname.includes('/trace')) {
      traceRequests.push(url);
    }
    await route.continue();
  });

  await page.goto(`/p/${pid}/s/${sheetId}/row/${farRow.id}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible({ timeout: 20_000 });
  await expect(drawer.getByTestId('row-field-title')).toContainText('Scale row 1500');

  expect(new Set(locateRequests.map(String)).size).toBe(1);
  expect(locateRequests.length).toBeLessThanOrEqual(2);
  for (const url of locateRequests) {
    expect(url.searchParams.get('page_size')).toBe('500');
  }
  const dataOffsets = dataRequests.map((url) => url.searchParams.get('offset'));
  expect(dataOffsets).toContain('1500');
  expect(dataOffsets).not.toContain('500');
  expect(dataOffsets).not.toContain('1000');
  for (const url of dataRequests) {
    const limit = Number(url.searchParams.get('limit') ?? 0);
    expect(limit).toBeLessThanOrEqual(500);
  }
  expect(reviewCountRequests.length).toBeGreaterThan(0);
  expect(reviewQueueRequests).toEqual([]);
  expect(traceRequests).toEqual([]);

  await page.getByLabel('Close drawer').click();
  await expect(drawer).not.toBeVisible();

  const filterResponse = page.waitForResponse((response) => {
    if (response.request().method() !== 'GET') return false;
    const url = new URL(response.url());
    return pathMatches(url, `/api/projects/${pid}/sheets/${sheetId}/data`) &&
      url.searchParams.has('filter');
  });
  await openFriendlyFilterSidebar(page, columns, 'status');
  await page.getByTestId('facet-check-status-open').check();
  const filteredUrl = new URL((await filterResponse).url());
  expect(filteredUrl.searchParams.get('limit')).toBe('500');

  const sortResponse = page.waitForResponse((response) => {
    if (response.request().method() !== 'GET') return false;
    const url = new URL(response.url());
    return pathMatches(url, `/api/projects/${pid}/sheets/${sheetId}/data`) &&
      url.searchParams.has('sort');
  });
  await openAdvancedSortPanel(page, columns, 'rank');
  await page.getByTestId('grid-sort-column').selectOption('rank');
  await page.getByTestId('grid-sort-direction').selectOption('desc');
  await page.getByTestId('apply-grid-sort').click();
  const sortedUrl = new URL((await sortResponse).url());
  expect(sortedUrl.searchParams.get('limit')).toBe('500');

  await openHistory(page);
  const panel = page.getByTestId('bottom-dock-panel');
  await expect(panel.getByTestId('history-load-older')).toBeVisible();
  historyRequests.length = 0;
  await panel.getByTestId('history-load-older').click();
  await expect.poll(() => historyRequests.some((url) => (
    url.searchParams.get('offset') === '0' &&
    url.searchParams.get('limit') === '50'
  ))).toBe(true);

  await openToolbarOverflow(page);
  await page.getByTestId('open-provenance-manifest').click();
  await expect(page.getByTestId('provenance-manifest')).toBeVisible({ timeout: 15_000 });
  expect(provenanceRequests.some((url) => (
    url.searchParams.get('runs_offset') === '0' &&
    url.searchParams.get('runs_limit') === '25' &&
    url.searchParams.get('receipts_offset') === '0' &&
    url.searchParams.get('receipts_limit') === '25'
  ))).toBe(true);

  expect(consoleErrors).toEqual([]);
  expect(pageErrors).toEqual([]);
  // `/api/me` and `/api/instance` are hosted-tier routes the shell probes to
  // decide which tier it is on; the local tier answers 404 BY DESIGN
  // (web/src/shellIdentity.ts, web/src/instanceIdentity.ts), so neither is a
  // page defect. The allowlist named only the first and went stale when the
  // second probe was added.
  const TIER_PROBES = ['/api/me', '/api/instance'];
  expect(
    httpErrors.filter(
      (error) => !(error.status === 404 && TIER_PROBES.includes(error.pathname)),
    ),
  ).toEqual([]);
});
