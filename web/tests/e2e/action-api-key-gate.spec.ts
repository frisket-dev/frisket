import { expect, test } from '@playwright/test';
import { openAction, openProject, seedGeoSheet } from './helpers';

// action-api-key-gate-v1: census enrichment declares CENSUS_API_KEY as a
// required credential (contracts/actions/definitions/enrichment.py). No key
// is configured for a fresh e2e project, so ActionPanel shows a proactive
// needs-credential banner with a Settings deep link (backend enforcement —
// distinct from the cost-gate 402 — is pinned by tests/
// test_action_api_key_gate.py; this spec pins the panel-side banner).

test('census enrichment shows a needs-credential banner with a Settings deep link', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoSheet(page, {
    namePrefix: 'e2e-census-key-gate',
    point: { lat: 38.9, lon: -77.03 },
  });

  await openProject(page, pid, sheetId);
  await openAction(page, 'enrich.census_demographics');

  const gate = page.getByTestId('action-credential-gate');
  await expect(gate).toBeVisible();
  await expect(gate).toContainText('CENSUS_API_KEY');
  await expect(gate).toContainText('Needs an API key');

  // The fixture has a valid geo_point source, so the accepted catalog's
  // missing credential is the sole readiness blocker. The controller-owned
  // reason must disable both preview and run before any backend round trip.
  await expect(page.getByRole('combobox', { name: 'Geo point column' })).toBeVisible();
  await expect(page.getByTestId('generated-action-run')).toBeDisabled();
  await expect(page.getByTestId('generated-action-preview')).toBeDisabled();
  await expect(page.getByTestId('generated-action-run')).toHaveAttribute('title', /CENSUS_API_KEY|credential/i);

  const settingsLink = page.getByTestId('action-credential-gate-settings-link');
  await expect(settingsLink).toBeVisible();
  await settingsLink.click();

  await expect(page).toHaveURL(
    new RegExp(`/p/${pid}/settings/project/secrets\\?secret=CENSUS_API_KEY$`),
  );
  await expect(page.getByTestId('project-secrets-settings')).toBeVisible();
});

test('the credential banner disappears once the project secret is configured', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoSheet(page, {
    namePrefix: 'e2e-census-key-gate-ok',
    point: { lat: 38.9, lon: -77.03 },
  });

  await page.request.post(`/api/projects/${pid}/secrets`, {
    data: { name: 'CENSUS_API_KEY', value: 'e2e-fake-key' },
  });

  await openProject(page, pid, sheetId);
  await openAction(page, 'enrich.census_demographics');

  await expect(page.getByTestId('action-credential-gate')).toHaveCount(0);
  await expect(page.getByTestId('generated-action-run')).toBeEnabled();
  await expect(page.getByTestId('generated-action-preview')).toBeEnabled();
});
