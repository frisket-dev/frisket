// RED-FIRST (authored 2026-06-13): model-call recipes should capture trace by
// default, and external-API recipes should not be described as purely local.

import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  uniqueName,
} from './helpers';

test('model recipes capture trace without an opt-in control', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-trace-default'));
  await importCsv(page.request, pid, 'rows.csv', 'note\n"alpha"\n');
  await page.goto(`/p/${pid}`);

  await openAction(page, 'map.classify');
  await expect(page.getByTestId('action-form')).toBeVisible();
  await expect(page.getByTestId('debug-trace-toggle')).toHaveCount(0);
});

test('geocode cost copy names the external API path instead of local zero-cost copy', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-geocode-cost-copy'));
  await importCsv(page.request, pid, 'rows.csv', 'address\n"10 Downing Street, London"\n');
  await page.goto(`/p/${pid}`);

  await openAction(page, 'enrich.geocode');
  await expect(page.getByTestId('generated-action-form')).toBeVisible();

  const cost = page.getByTestId('cost-estimate');
  await expect(cost).toBeVisible();
  await expect(cost).toContainText(/OpenCage|Nominatim|public API|external API/i);
  await expect(cost).not.toContainText(/runs locally, no model call/i);
});
