// Error remediation + in-app Diagnose: a server-unreachable ProjectPicker boot
// failure gets a "start it with `frisket <workspace>`" remediation line, and
// a run-confirm-time missing_provider_key failure gets named-provider
// remediation plus a working "Open Diagnose" link into the real
// GET /api/diagnose endpoint.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  uniqueName,
} from './helpers';

test('project picker server-unreachable error shows the start-it remediation', async ({ page }) => {
  await page.route('**/api/projects', async (route) => {
    if (route.request().method() === 'GET') {
      await route.abort('connectionrefused');
      return;
    }
    await route.continue();
  });

  await page.goto('/');
  await expect(page.getByTestId('home-screen')).toBeVisible();
  const error = page.getByTestId('picker-error');
  await expect(error).toBeVisible();
  await expect(error).toContainText('Cannot reach the frisket server');
  await expect(page.getByTestId('picker-error-remediation')).toContainText(
    'frisket <workspace>',
  );
});

test('missing provider key at run-confirm time shows named remediation and Diagnose link', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-error-remediation'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'remediation.csv',
    'headline,city\n"Budget approved","Berlin"\n',
  );

  await page.route(`**/api/projects/${pid}/actions/v1/estimate`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_estimate_result.v1',
        action: { kind: 'map.classify', action_id: 'remediation-estimate-e2e' },
        project_id: pid,
        estimate: {
          rows: 1,
          cost: 0.01,
          cost_source: 'estimated',
          billed_cost: 10_000,
          policy_id: 'frisket.pricing.identity.v1',
        },
      }),
    });
  });

  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    await route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'map.classify', action_id: 'remediation-run-e2e' },
        status: 'failed',
        project_id: pid,
        errors: [
          {
            schema_version: 'frisket.action_error.v1',
            code: 'missing_provider_key',
            message:
              "No API key is configured for 'anthropic'. Add one in Settings → AI Providers, or pick a different model.",
            action_kind: 'map.classify',
            field: 'params.model',
            details: { provider: 'anthropic' },
          },
        ],
      }),
    });
  });

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await openAction(page, 'map.classify');
  await expect(page.getByTestId('action-form')).toBeVisible();
  await expect(page.getByTestId('cost-estimate')).toContainText('$0.01');

  await page.getByTestId('run-button').click();

  const toast = page.getByTestId('error-toast');
  await expect(toast).toBeVisible();
  await expect(toast).toContainText('anthropic');
  await expect(page.getByTestId('error-toast-remediation')).toContainText(
    'No API key is configured for anthropic',
  );
  const diagnoseLink = page.getByTestId('error-toast-diagnose');
  await expect(diagnoseLink).toBeVisible();

  // "Open Diagnose" navigates to the
  // Settings > Diagnostics section instead of opening a modal.
  await diagnoseLink.click();
  await expect(page).toHaveURL(/\/settings\/personal\/diagnostics$/);
  const section = page.getByTestId('settings-diagnostics');
  await expect(section).toBeVisible();
  await expect(page.getByTestId('diagnose-panel-status')).toBeVisible({ timeout: 15_000 });
  await expect(section).toContainText(/model providers/i);
});
