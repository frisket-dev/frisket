// RED-FIRST (authored 2026-06-14): in-app report issue should generate a
// redacted diagnostic bundle with route/workspace/browser context.

import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';

function boxesOverlap(
  a: { x: number; y: number; width: number; height: number },
  b: { x: number; y: number; width: number; height: number },
): boolean {
  return a.x < b.x + b.width &&
    a.x + a.width > b.x &&
    a.y < b.y + b.height &&
    a.y + a.height > b.y;
}

test('report issue posts a redacted diagnostic bundle request', async ({ page }) => {
  let reportBody: Record<string, unknown> | null = null;
  let clientErrorBody: Record<string, unknown> | null = null;
  await page.addInitScript(() => {
    window.__FRISKET_CLIENT_ERROR_CAPTURE__ = true;
  });
  await page.route('**/api/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ email: 'admin@example.com' }),
    });
  });
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    });
  });
  await page.route('**/api/client-errors', async (route) => {
    clientErrorBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, id: 41 }),
    });
  });
  await page.route('**/api/diagnostic-bundle', async (route) => {
    reportBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        report_id: 7,
        bundle: {
          generated_at: '2026-06-14T12:00:00Z',
          context: { project_id: 'investigation', sheet_id: '12' },
          queue: { summary: { failed: 1 }, jobs: [] },
          recent_errors: [],
        },
      }),
    });
  });

  await page.goto('/');
  await page.evaluate(() => {
    window.history.replaceState(
      null,
      '',
      '/p/investigation/s/12?token=sk-secret-token&raw_cells=private-note',
    );
  });
  await page.evaluate(() => {
    window.dispatchEvent(new ErrorEvent('error', {
      message: 'Grid crashed before report issue',
      error: new Error('Grid crashed before report issue'),
    }));
  });
  await expect.poll(() => clientErrorBody).not.toBeNull();
  const reportButton = page.getByTestId('report-issue-button');
  const reportButtonBox = await reportButton.boundingBox();
  const viewport = page.viewportSize();
  expect(reportButtonBox).not.toBeNull();
  expect(viewport).not.toBeNull();
  expect(reportButtonBox!.y + reportButtonBox!.height).toBeLessThan(viewport!.height - 34);
  await reportButton.click();
  await expect(page.getByTestId('diagnostic-popover')).toBeVisible();
  await expect(page.getByTestId('diagnostic-include-raw')).toHaveCount(0);
  const description = page.getByTestId('diagnostic-description');
  const submit = page.getByTestId('diagnostic-submit');
  await expect(description).toHaveAttribute('maxlength', '1000');
  await expect(submit).toBeDisabled();
  await description.fill('   ');
  await expect(submit).toBeDisabled();
  await description.fill('x'.repeat(1001));
  await expect(description).toHaveValue('x'.repeat(1000));
  await expect(submit).toBeEnabled();
  await description.fill('  The grid froze after I sorted the date column.  ');
  await expect(submit).toBeEnabled();
  await submit.click();

  await expect(page.getByRole('status')).toContainText('Report sent #7');
  expect(reportBody).not.toBeNull();
  const body = reportBody!;
  const context = body.context as Record<string, unknown>;
  const serialized = JSON.stringify(body);
  expect(body.route).toBe('/p/investigation/s/12');
  expect(body.message).toBe('The grid froze after I sorted the date column.');
  expect(Object.prototype.hasOwnProperty.call(body, 'include_raw_values')).toBe(true);
  expect(body.include_raw_values).toBe(false);
  expect(body.includeRawValues).toBeUndefined();
  expect(body.recent_client_error_ids).toEqual([41]);
  expect(context.project_id).toBe('investigation');
  expect(context.sheet_id).toBe('12');
  expect(JSON.stringify(clientErrorBody)).not.toContain('private-note');
  expect(serialized).not.toContain('sk-secret-token');
  expect(serialized).not.toContain('private-note');
});

test('report issue keeps the description when sending fails', async ({ page }) => {
  await page.route('**/api/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ email: 'admin@example.com' }),
    });
  });
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    });
  });
  await page.route('**/api/diagnostic-bundle', async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Could not send report.' }),
    });
  });

  await page.goto('/');
  await page.getByTestId('report-issue-button').click();
  const description = page.getByTestId('diagnostic-description');
  await description.fill('The grid froze after I sorted the date column.');
  await page.getByTestId('diagnostic-submit').click();

  await expect(page.getByRole('alert')).toContainText('Could not send report.');
  await expect(description).toHaveValue('The grid froze after I sorted the date column.');
});

test('report issue survives malformed encoded route segments', async ({ page }) => {
  let reportBody: Record<string, unknown> | null = null;
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.route('**/api/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ email: 'admin@example.com' }),
    });
  });
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    });
  });
  await page.route('**/api/diagnostic-bundle', async (route) => {
    reportBody = route.request().postDataJSON();
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        ok: true,
        report_id: 8,
        bundle: {
          generated_at: '2026-06-18T12:00:00Z',
          context: {},
          queue: { summary: {}, jobs: [] },
          recent_errors: [],
        },
      }),
    });
  });

  await page.goto('/');
  await page.evaluate(() => {
    window.history.replaceState(
      null,
      '',
      '/p/%E0%A4%A/s/12?token=sk-secret-token&raw_cells=private-note',
    );
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await page.getByTestId('report-issue-button').click();
  await expect(page.getByTestId('diagnostic-popover')).toBeVisible();
  await page.getByTestId('diagnostic-description').fill('The page stopped responding.');
  await page.getByTestId('diagnostic-submit').click();

  await expect(page.getByTestId('diagnostic-sent')).toContainText('Report sent #8');
  expect(reportBody).not.toBeNull();
  const body = reportBody!;
  const context = body.context as Record<string, unknown>;
  const serialized = JSON.stringify(body);
  expect(body.route).toBe('/p/%E0%A4%A/s/12');
  expect(context.route).toBe('/p/%E0%A4%A/s/12');
  expect(context.route_parse_error).toBe('malformed_percent_encoding');
  expect(context.project_id).toBeUndefined();
  expect(context.sheet_id).toBeUndefined();
  expect(serialized).not.toContain('sk-secret-token');
  expect(serialized).not.toContain('private-note');
  expect(pageErrors.some((message) => message.includes('URI malformed'))).toBe(false);
});

test('report issue button does not overlap footer undo redo or review controls', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-report-issue-position'));
  await importCsv(page.request, pid, 'rows.csv', 'name\nAlbany\n');
  await page.route(`**/api/projects/${pid}/review/count`, (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ count: 1 }),
  }));
  await page.route('**/api/me', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ email: 'admin@example.com' }),
    });
  });

  await page.goto(`/p/${pid}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  const report = await page.getByTestId('report-issue-button').boundingBox();
  const undo = await page.getByTestId('undo-button').boundingBox();
  const redo = await page.getByTestId('redo-button').boundingBox();
  const review = await page.getByTestId('review-queue-button').boundingBox();
  expect(report).not.toBeNull();
  expect(undo).not.toBeNull();
  expect(redo).not.toBeNull();
  expect(review).not.toBeNull();
  expect(boxesOverlap(report!, undo!)).toBe(false);
  expect(boxesOverlap(report!, redo!)).toBe(false);
  expect(boxesOverlap(report!, review!)).toBe(false);
});
