// RED-FIRST (authored 2026-06-14): operators need a recent-errors feed in
// /admin so normal browser/backend/worker diagnosis does not require SSH.

import { expect, test } from '@playwright/test';

test('admin page exposes recent browser and worker errors', async ({ page }) => {
  await page.route('**/api/admin/overview', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        orgs: [],
        totals: { orgs: 0 },
      }),
    });
  });
  await page.route('**/api/admin/browser/jobs', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ summary: {}, jobs: [] }),
    });
  });
  await page.route('**/api/admin/browser/errors?*', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        errors: [
          {
            id: 'client:9',
            record_id: 9,
            kind: 'browser',
            source: 'browser',
            severity: 'error',
            name: 'TypeError',
            message: "Cannot read properties of undefined (reading 'toFixed')",
            stack: null,
            route: '/p/investigation/s/12?token=sk-secret-token#trace',
            org_id: 1,
            user_id: 1,
            project_id: 'investigation',
            sheet_id: '12',
            run_id: '77',
            job_id: null,
            trace_id: 'trace-abc',
            context: { browser: 'Chromium' },
            at: '2026-06-14T12:00:00Z',
          },
          {
            id: 'job:42',
            kind: 'job',
            source: 'worker',
            severity: 'error',
            name: 'source.poll',
            message: 'feed response exceeded 52428800 bytes',
            stack: null,
            route: null,
            org_id: 1,
            user_id: null,
            project_id: 'sources',
            sheet_id: null,
            run_id: null,
            job_id: '42',
            trace_id: null,
            context: { status: 'failed' },
            at: '2026-06-14T11:59:00Z',
          },
          {
            id: 'client:10',
            record_id: 10,
            kind: 'diagnostic',
            source: 'diagnostic_report',
            severity: 'info',
            name: 'diagnostic_bundle',
            message: 'diagnostic_bundle: user report',
            stack: null,
            route: '/p/investigation/s/12',
            org_id: 1,
            user_id: 1,
            project_id: 'investigation',
            sheet_id: '12',
            run_id: null,
            job_id: null,
            trace_id: 'diag-trace',
            context: { project_id: 'investigation', raw_cells: '[redacted]' },
            bundle: {
              route: '/p/investigation/s/12',
              context: { project_id: 'investigation', raw_cells: '[redacted]' },
              recent_errors: [{ message: 'recent browser crash' }],
            },
            at: '2026-06-14T11:58:00Z',
          },
          {
            id: 'client:11',
            record_id: 11,
            kind: 'browser',
            source: 'browser',
            severity: 'error',
            name: 'RouteError',
            message: 'legacy unsafe route',
            stack: null,
            route: 'javascript:alert(1)',
            org_id: 1,
            user_id: 1,
            project_id: null,
            sheet_id: null,
            run_id: null,
            job_id: null,
            trace_id: null,
            context: {},
            at: '2026-06-14T11:57:00Z',
          },
        ],
      }),
    });
  });

  await page.goto('/admin/errors');
  const panel = page.getByTestId('admin-errors-panel');
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId('admin-error-count')).toContainText('4');
  const table = panel.getByTestId('admin-errors-table');
  await expect(table).toContainText('browser');
  await expect(table).toContainText('worker');
  await expect(table).toContainText('diagnostic');
  await expect(table).toContainText('toFixed');
  await expect(table).toContainText('project investigation · sheet 12 · run 77');
  await expect(table).toContainText('trace-abc');
  await expect(table).toContainText('feed response exceeded 52428800 bytes');
  const diagnosticBundle = table.getByTestId('admin-error-bundle-10');
  await diagnosticBundle.getByText('Diagnostic bundle').click();
  await expect(diagnosticBundle).toContainText('recent browser crash');
  await expect(diagnosticBundle).toContainText('[redacted]');
  await expect(table.getByRole('link', { name: 'Open' }).first()).toHaveAttribute(
    'href',
    '/p/investigation/s/12',
  );
  const unsafeRouteRow = table.getByRole('row').filter({ hasText: 'legacy unsafe route' });
  await expect(unsafeRouteRow.getByRole('link', { name: 'Open' })).toHaveCount(0);
});
