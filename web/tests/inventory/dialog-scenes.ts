import { expect, type Page } from '@playwright/test';
import { createProject, importCsv, openDiscoverTab, openProject } from '../e2e/helpers';

type Shot = (page: Page, family: string, name: string, source: string) => Promise<void>;

async function expectNativeDialog(page: Page, testId: string) {
  const dialog = page.getByTestId(testId);
  await expect(dialog).toHaveJSProperty('open', true);
  expect(await dialog.evaluate(element => element.tagName)).toBe('DIALOG');
  for (let index = 0; index < 3; index++) {
    await page.keyboard.press('Shift+Tab');
    expect(await dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  }
}

async function dismissWithEscape(page: Page, testId: string) {
  await expectNativeDialog(page, testId);
  await page.keyboard.press('Escape');
  await expect(page.getByTestId(testId)).toHaveCount(0);
}

/** Actual app components. Only the config/OAuth availability responses are synthetic;
 * no export, model execution, provider request, or telemetry submission is made. */
export async function captureDialogScenes(page: Page, shot: Shot) {
  const pid = await createProject(page.request, 'Dialog inventory');
  const sid = await importCsv(page.request, pid, 'records.csv', 'name,notes\nAda,Council meeting\nMax,Planning hearing\n');
  await importCsv(page.request, pid, 'meetings.csv', 'date,attendance\n2026-10-01,42\n');
  const added = await page.request.post(`/api/projects/${pid}/actions/v1/run`, { data: {
    action_id: 'column.add', scope: { kind: 'project' },
    params: { sheet_id: Number(sid), name: 'document_tables', type: 'json' },
    output_names: {}, idempotency_key: `inventory-json-column-${pid}`,
  } });
  expect(added.ok()).toBeTruthy();
  expect((await added.json()).status).toBe('completed');
  await openProject(page, pid, sid);
  await page.getByTestId('ribbon-tab-data').click();
  await page.getByTestId('ribbon-command-export-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await shot(page, 'Dialogs', 'export-data', 'src/components/TopNav.tsx');
  await dismissWithEscape(page, 'export-data-modal');

  await page.getByTestId('ribbon-command-export-google-sheets').click();
  await expect(page.getByTestId('export-google-sheets-unavailable')).toBeVisible();
  await shot(page, 'Dialogs', 'google-sheets-unavailable-local', 'src/components/TopNav.tsx');
  await dismissWithEscape(page, 'export-google-sheets-modal');

  await page.getByTestId('ribbon-command-export-column-tables').click();
  await expect(page.getByTestId('export-column-tables-column')).toBeVisible();
  await page.getByTestId('export-column-tables-group-by').fill('table_index');
  await page.getByTestId('export-column-tables-exclude-columns').fill('source_row_id\nsource_filename');
  await shot(page, 'Dialogs', 'export-column-tables', 'src/components/ExportColumnTablesModal.tsx');
  const columnSelect = page.getByTestId('export-column-tables-column');
  await columnSelect.click();
  await expect(page.getByRole('listbox')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox')).toHaveCount(0);
  await expect(page.getByTestId('export-column-tables-modal')).toBeVisible();
  await expect(columnSelect).toBeFocused();
  await dismissWithEscape(page, 'export-column-tables-modal');

  await openDiscoverTab(page, 'Embeddings');
  await page.getByTestId('embeddings-add-button').click();
  await expect(page.getByTestId('embedding-source-col-notes')).toBeVisible();
  await expect(page.getByTestId('embeddings-create-modal').locator('.engine-selector-field-loading')).toHaveCount(0);
  await shot(page, 'Dialogs', 'embedding-index-create', 'src/components/embeddings/CreateEmbeddingIndexDialog.tsx');
  await dismissWithEscape(page, 'embeddings-create-modal');

  await page.route('**/actions/v1/catalog', async route => {
    const response = await route.fetch();
    const catalog = await response.json();
    for (const action of catalog.actions) {
      if (action.kind === 'export.google_sheets') delete action.ui_hints.unavailable_reason;
    }
    await route.fulfill({ response, json: catalog });
  });
  await page.route('**/api/org/oauth/connections*', route => route.fulfill({ json: {
    connections: [{ id: 'inventory-google', provider: 'google', external_email: 'reporter@example.test' }],
  } }));
  let exportRequests = 0;
  await page.route('**/actions/v1/run', async route => {
    exportRequests++;
    await route.abort();
  });
  await openProject(page, pid, sid);
  await page.getByTestId('ribbon-tab-data').click();
  await page.getByTestId('ribbon-command-export-google-sheets').click();
  await expect(page.getByLabel('Google account', { exact: true })).toContainText('reporter@example.test');
  await expect(page.getByTestId('export-google-sheets-submit')).toBeEnabled();
  await shot(page, 'Dialogs', 'google-sheets-connected', 'src/components/TopNav.tsx (synthetic connected account and available catalog; export not sent)');
  await dismissWithEscape(page, 'export-google-sheets-modal');
  expect(exportRequests).toBe(0);

  let configWrites = 0;
  await page.route('**/api/config', async route => {
    if (route.request().method() !== 'GET') {
      configWrites++;
      return route.abort();
    }
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), cache_mode: 'replay_strict', cache_mode_editable: true } });
  });
  await page.goto('/settings/personal/preferences');
  await page.getByTestId('ai-call-mode-replay').getByRole('radio').check();
  await page.getByTestId('ai-call-mode-save').click();
  await expect(page.getByTestId('ai-call-mode-confirmation')).toBeVisible();
  await shot(page, 'Dialogs', 'ai-call-mode-confirmation', 'src/settings/SettingsSections.tsx (synthetic editable runtime config)');
  await expectNativeDialog(page, 'ai-call-mode-confirmation');
  await page.getByTestId('ai-call-mode-confirm-cancel').click();
  await expect(page.getByTestId('ai-call-mode-confirmation')).toHaveCount(0);
  expect(configWrites).toBe(0);
  await page.getByTestId('ai-call-mode-save').click();
  await dismissWithEscape(page, 'ai-call-mode-confirmation');
  expect(configWrites).toBe(0);
  await page.unroute('**/api/config');

  await page.route('**/api/config', async route => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), product_telemetry_available: true } });
  });
  await page.evaluate(() => localStorage.removeItem('frisket:product-telemetry:v1'));
  await openProject(page, pid, sid);
  await expect(page.getByTestId('product-telemetry-disclosure')).toBeVisible();
  await shot(page, 'Dialogs', 'telemetry-disclosure', 'src/telemetry/ProductTelemetryProvider.tsx (synthetic telemetry availability; no events sent)');
  await expectNativeDialog(page, 'product-telemetry-disclosure');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('product-telemetry-disclosure')).toHaveJSProperty('open', true);
  await page.getByRole('checkbox', { name: 'Send product telemetry' }).uncheck();
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  await expect(page.getByTestId('product-telemetry-disclosure')).toHaveCount(0);
}
