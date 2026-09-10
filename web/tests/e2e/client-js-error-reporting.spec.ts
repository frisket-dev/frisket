// RED-FIRST (authored 2026-06-14): browser exceptions should be captured with
// redacted route/project/sheet context, without relying on pasted console logs.

import { expect, test } from '@playwright/test';

const GENERIC_PADDED_TOKEN = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij123456==';

const dispatchBrowserError = async (page: import('@playwright/test').Page) => {
  await page.evaluate((genericToken) => {
    window.history.replaceState(
      null,
      '',
      '/p/investigation/s/12?token=sk-secret-token&raw_cells=private-note',
    );
    const err = new Error("Cannot read properties of undefined (reading 'toFixed')");
    err.stack = `TypeError: bad
    at http://localhost:5173/assets/app.js?token=sk-secret-token
    opaque ${genericToken}`;
    window.dispatchEvent(new ErrorEvent('error', { message: err.message, error: err }));
  }, GENERIC_PADDED_TOKEN);
};

const fulfillSyntheticAppShell = async (
  route: import('@playwright/test').Route,
  localBaseURL: string,
) => {
  const url = new URL(route.request().url());
  if (url.pathname === '/' || url.pathname === '/index.html') {
    await route.fulfill({
      status: 200,
      contentType: 'text/html',
      body: `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>frisket</title>
  </head>
  <body>
    <div id="root"></div>
    <div id="portal"></div>
    <script type="module" src="${new URL('/src/main.tsx', localBaseURL)}"></script>
  </body>
</html>`,
    });
    return;
  }
  await route.fulfill({ status: 404, body: 'not found' });
};

test('browser exception capture is disabled by default on local/self-host builds', async ({ page }) => {
  const reports: unknown[] = [];
  await page.route('**/api/client-errors', async (route) => {
    reports.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, id: 10 }),
    });
  });

  await page.goto('/');
  await dispatchBrowserError(page);

  await page.waitForTimeout(300);
  expect(reports).toHaveLength(0);
});

test('browser exceptions are posted with redacted workspace context when runtime capture is enabled', async ({
  page,
}) => {
  const reports: unknown[] = [];
  await page.addInitScript(() => {
    (
      window as Window & { __FRISKET_CLIENT_ERROR_CAPTURE__?: boolean }
    ).__FRISKET_CLIENT_ERROR_CAPTURE__ = true;
  });
  await page.route('**/api/client-errors', async (route) => {
    reports.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, id: 10 }),
    });
  });

  await page.goto('/');
  await dispatchBrowserError(page);

  await expect.poll(() => reports.length).toBe(1);
  const report = reports[0] as {
    message: string;
    stack: string;
    route: string;
    context: Record<string, unknown>;
  };
  const serialized = JSON.stringify(report);
  expect(report.message).toContain('toFixed');
  expect(report.route).toBe('/p/investigation/s/12');
  expect(report.context.project_id).toBe('investigation');
  expect(report.context.sheet_id).toBe('12');
  expect(serialized).not.toContain('sk-secret-token');
  expect(serialized).not.toContain('private-note');
  expect(serialized).not.toContain(GENERIC_PADDED_TOKEN);
});

test('browser exception capture survives malformed encoded route segments', async ({ page }) => {
  const reports: unknown[] = [];
  type ClientErrorReport = {
    message: string;
    route: string;
    context: Record<string, unknown>;
  };
  await page.addInitScript(() => {
    (
      window as Window & { __FRISKET_CLIENT_ERROR_CAPTURE__?: boolean }
    ).__FRISKET_CLIENT_ERROR_CAPTURE__ = true;
  });
  await page.route('**/api/client-errors', async (route) => {
    reports.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, id: 11 }),
    });
  });
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    });
  });

  await page.goto('/');
  await page.evaluate(() => {
    window.history.replaceState(
      null,
      '',
      '/p/%E0%A4%A/s/12?token=sk-secret-token&raw_cells=private-note',
    );
    const err = new Error('Malformed route crash token=sk-secret-token');
    window.dispatchEvent(new ErrorEvent('error', { message: err.message, error: err }));
  });

  await expect.poll(() => reports.length).toBe(1);
  const report = reports[0] as ClientErrorReport;
  const serialized = JSON.stringify(report);
  expect(report.message).toContain('Malformed route crash');
  expect(report.route).toBe('/p/%E0%A4%A/s/12');
  expect(report.context.route).toBe('/p/%E0%A4%A/s/12');
  expect(report.context.project_id).toBeUndefined();
  expect(report.context.sheet_id).toBeUndefined();
  expect(report.context.route_parse_error).toBe('malformed_percent_encoding');
  expect(serialized).not.toContain('URIError');
  expect(serialized).not.toContain('sk-secret-token');
  expect(serialized).not.toContain('private-note');

  await page.evaluate(() => {
    window.history.replaceState(
      null,
      '',
      '/p/investigation/s/%E0%A4%A?token=sk-secret-token',
    );
    const err = new Error('Malformed sheet route crash');
    window.dispatchEvent(new ErrorEvent('error', { message: err.message, error: err }));
  });
  await expect.poll(() => reports.length).toBe(2);
  const malformedSheetReport = reports[1] as ClientErrorReport;
  const malformedSheetSerialized = JSON.stringify(malformedSheetReport);
  expect(malformedSheetReport.route).toBe('/p/investigation/s/%E0%A4%A');
  expect(malformedSheetReport.context.route).toBe('/p/investigation/s/%E0%A4%A');
  expect(malformedSheetReport.context.project_id).toBe('investigation');
  expect(malformedSheetReport.context.sheet_id).toBeUndefined();
  expect(malformedSheetReport.context.route_parse_error).toBe('malformed_percent_encoding');
  expect(malformedSheetSerialized).not.toContain('sk-secret-token');

  await page.evaluate(() => {
    window.history.replaceState(
      null,
      '',
      '/p/investigation/s/12/%E0%A4%A?token=sk-secret-token',
    );
    const err = new Error('Malformed tail route crash');
    window.dispatchEvent(new ErrorEvent('error', { message: err.message, error: err }));
  });
  await expect.poll(() => reports.length).toBe(3);
  const malformedTailReport = reports[2] as ClientErrorReport;
  const malformedTailSerialized = JSON.stringify(malformedTailReport);
  expect(malformedTailReport.route).toBe('/p/investigation/s/12/%E0%A4%A');
  expect(malformedTailReport.context.route).toBe('/p/investigation/s/12/%E0%A4%A');
  expect(malformedTailReport.context.project_id).toBe('investigation');
  expect(malformedTailReport.context.sheet_id).toBe('12');
  expect(malformedTailReport.context.route_parse_error).toBe('malformed_percent_encoding');
  expect(malformedTailSerialized).not.toContain('sk-secret-token');
});

test('app bootstrap survives malformed encoded route segments', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await page.route('**/api/projects', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify([]),
    });
  });

  await page.goto('/');
  await page.evaluate(() => {
    window.history.replaceState(
      null,
      '',
      '/p/%E0%A4%A/s/12?token=sk-secret-token',
    );
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await page.waitForTimeout(300);

  await expect(page.getByText('Something went wrong.')).toHaveCount(0);
  await expect(page).toHaveURL(/\/p\/%E0%A4%A\/s\/12\?token=sk-secret-token$/);
  expect(pageErrors.some((message) => message.includes('URI malformed'))).toBe(false);
});

test('manual query opt-in enables browser capture for the session', async ({ page }) => {
  const reports: unknown[] = [];
  await page.route('**/api/client-errors', async (route) => {
    reports.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, id: 10 }),
    });
  });

  await page.goto('/?frisket_client_errors=1');
  await dispatchBrowserError(page);
  await expect.poll(() => reports.length).toBe(1);

  await page.goto('/');
  await dispatchBrowserError(page);
  await expect.poll(() => reports.length).toBe(2);

  await page.goto('/?frisket_client_errors=maybe');
  await dispatchBrowserError(page);
  await expect.poll(() => reports.length).toBe(3);

  await page.goto('/?frisket_client_errors=0');
  await dispatchBrowserError(page);
  await page.waitForTimeout(300);
  expect(reports).toHaveLength(3);

  await page.goto('/');
  await dispatchBrowserError(page);
  await page.waitForTimeout(300);
  expect(reports).toHaveLength(3);
});

test('manual query opt-in is ignored on shared origins', async ({ page }, testInfo) => {
  const localBaseURL = String(testInfo.project.use.baseURL ?? 'http://127.0.0.1:5173');
  const reports: unknown[] = [];
  await page.route('**/api/client-errors', async (route) => {
    reports.push(route.request().postDataJSON());
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ ok: true, id: 10 }),
    });
  });
  await page.route('http://frisket.example/**', async (route) => {
    await fulfillSyntheticAppShell(route, localBaseURL);
  });

  await page.goto('http://frisket.example/?frisket_client_errors=1');
  await dispatchBrowserError(page);

  await page.waitForTimeout(300);
  expect(reports).toHaveLength(0);
});
