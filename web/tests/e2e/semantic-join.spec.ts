// Semantic join UI: action-panel form posts a translated v1 action and refreshes
// after the completed action. The backend semantic executor is covered by Python
// tests; this browser spec stays deterministic when the local embedder extra is
// not installed.

import { expect, test } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  importCsv,
  listSheets,
  openAction,
  openProject,
  uniqueName,
} from './helpers';

test('semantic join form creates a linkage child sheet', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('semantic-join'));
  const donors = await importCsv(
    page.request,
    pid,
    'donors.csv',
    'donor,location\n"ACME Corp","Boston"\n"Globex","NYC"\n',
  );
  const registry = await page.request.post(`/api/projects/${pid}/import/csv?sheet_name=registry`, {
    multipart: {
      file: {
        name: 'registry.csv',
        mimeType: 'text/csv',
        buffer: Buffer.from('company\n"Acme Corporation"\n"Globex LLC"\n'),
      },
    },
  });
  expect(registry.ok()).toBeTruthy();
  const registryBody = await registry.json() as { sheet_id: number };
  let postedAction: Record<string, unknown> | null = null;

  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    postedAction = route.request().postDataJSON() as Record<string, unknown>;
    if (postedAction.kind !== 'join.semantic') {
      await route.continue();
      return;
    }

    const child = await page.request.post(`/api/projects/${pid}/import/csv?sheet_name=Linkage`, {
      multipart: {
        file: {
          name: 'linkage.csv',
          mimeType: 'text/csv',
          buffer: Buffer.from(
            'donor,semantic_match,semantic_match_score,semantic_match_target_row_id\n' +
            '"ACME Corp","Acme Corporation","0.98","1"\n' +
            '"Globex","Globex LLC","0.97","2"\n',
          ),
        },
      },
    });
    if (!child.ok()) {
      await route.fulfill({ status: 500, body: await child.text() });
      return;
    }
    const childBody = await child.json() as { sheet_id: number };
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'join.semantic', action_id: 'act-web-semantic-join' },
        status: 'completed',
        project_id: pid,
        run_id: null,
        job_id: null,
        op_ids: [],
        outputs: [{ kind: 'sheet', sheet_id: childBody.sheet_id, name: 'Linkage' }],
        receipt_id: 'receipt-web-semantic-join',
        warnings: [],
        errors: [],
      }),
    });
  });

  await openProject(page, pid, donors);
  await openAction(page, 'join.semantic');
  await expect(page.getByTestId('action-form')).toBeVisible();
  await expect(page.getByTestId('semantic-join-form')).toBeVisible();
  await expect(page.getByTestId('media-source-column-select')).toBeVisible();
  await expect(page.getByTestId('media-source-binding')).toHaveCount(1);

  // Target sheet/column are real dropdowns (design card 5b), populated from
  // the project's sheet list (api.listSheets()) once it loads.
  await expect(
    page.locator('[data-testid="field-target_sheet"] option[value="registry"]'),
  ).toHaveCount(1);
  await page.getByTestId('field-target_sheet').selectOption('registry');
  await expect(
    page.locator('[data-testid="field-target_column"] option[value="company"]'),
  ).toHaveCount(1);
  await page.getByTestId('field-target_column').selectOption('company');

  // Columns to carry over: a chip multi-select (MultiColumnPicker) sourced
  // from this sheet's columns, excluding the join's source column.
  const carryPicker = page.getByTestId('field-carry_columns');
  await carryPicker.click();
  await expect(
    page.getByTestId('field-carry_columns-menu').getByRole('option', { name: /^donor/ }),
  ).toHaveCount(0);
  await page.getByTestId('field-carry_columns-menu').getByRole('option', { name: /^location/ }).click();
  await expect(carryPicker).toContainText('location');
  // Dismiss the carry-columns dropdown with a pointerdown inside the form (a
  // click outside the picker closes it). Escape would close the overlay action
  // drawer itself (workbench-ia-action-drawer-v1), not just the menu.
  await page.getByTestId('semantic-join-form').getByText('Columns to carry over').click();
  await expect(page.getByTestId('field-carry_columns-menu')).toBeHidden();

  await page.getByTestId('field-child_sheet').fill('Linkage');

  // Match/confident thresholds render side by side.
  await page.getByTestId('field-match_threshold').fill('0.10');
  await page.getByTestId('field-confident_threshold').fill('0.95');

  const response = page.waitForResponse((r) =>
    r.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
    r.request().method() === 'POST',
  );
  await clickRunButton(page);
  const out = await response;
  const bodyText = await out.text();
  expect(out.ok(), bodyText).toBeTruthy();
  const result = JSON.parse(bodyText);
  expect(result.schema_version).toBe('frisket.action_result.v1');
  expect(result.action.kind).toBe('join.semantic');
  expect(result.receipt_id).toBeTruthy();
  expect(postedAction?.capabilities).toEqual(['project:write', 'model:embed']);
  const params = postedAction?.params as Record<string, unknown>;
  expect(params.sheet_id).toBe(donors);
  expect(params.input_columns).toEqual(['donor']);
  expect(params.target_sheet_id).toBe(registryBody.sheet_id);
  expect(params.target_sheet).toBeUndefined();
  expect(params.target_column).toBe('company');
  expect(params.output_name).toBe('semantic_match');
  expect(params.child_sheet_name).toBe('Linkage');
  expect(params.match_threshold).toBe(0.10);
  expect(params.confident_threshold).toBe(0.95);
  expect(params.carry_columns).toEqual(['location']);
  expect(params.confirmed).toBe(false);
  expect(params.source_column).toBeUndefined();

  await expect(page.getByTestId('workbench-mainView-tab-3')).toContainText('Linkage', { timeout: 30_000 });
  const sheets = await listSheets(page.request, pid);
  expect(sheets.some((s) => s.name === 'Linkage')).toBeTruthy();
});
