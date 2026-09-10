import { expect, test, type APIRequestContext } from '@playwright/test';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  createProject,
  importCsv,
  openCellDrawer,
  openProject,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

const PLUGIN_ID = 'demo.stars';
const PLUGIN_CAPABILITY = 'plugin:trusted_local_backend';
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const PLUGIN_MANIFEST = resolve(ROOT, 'tests/fixtures/local_plugins/demo_stars/plugin.json');

async function activateStarsPlugin(request: APIRequestContext, pid: string): Promise<void> {
  const load = await request.post(`/api/projects/${pid}/actions/v1/run`, {
    data: {
      action_id: 'plugin.load',
      scope: { kind: 'project' },
      params: {
        manifest: {
          kind: 'local_file',
          path: PLUGIN_MANIFEST,
        },
      },
      idempotency_key: `plugin-stars-e2e@sha256:${Date.now()}`,
    },
  });
  expect(load.ok()).toBeTruthy();
  const loadResult = await load.json() as { status: string; receipt_id?: string };
  expect(loadResult.status).toBe('completed');
  expect(loadResult.receipt_id).toBeTruthy();

  const enabled = await request.post(`/api/projects/${pid}/workbench/plugins/${PLUGIN_ID}/activate`, {
    data: {
      receiptId: loadResult.receipt_id,
      trustAcknowledged: true,
      permissionsAccepted: [PLUGIN_CAPABILITY],
      arbitraryPackageLoadAllowed: false,
    },
  });
  expect(enabled.ok()).toBeTruthy();

  const backend = await request.post(
    `/api/projects/${pid}/workbench/plugins/${PLUGIN_ID}/backend/activate`,
    { data: { trustAcknowledged: true, arbitraryPackageLoadAllowed: false } },
  );
  expect(backend.ok()).toBeTruthy();
  const body = await backend.json() as {
    registeredBackendContributions: { columnTypes: string[] };
  };
  expect(body.registeredBackendContributions.columnTypes).toEqual(['stars']);
}

test('demo_stars registers a stars renderer, displays stars, and edits numerically', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('plugin-stars'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'ratings.csv',
    'title,rating\nAlpha,4.5\nBeta,2\n',
  );

  await activateStarsPlugin(page.request, pid);

  const registry = await page.request.get(`/api/projects/${pid}/column-types`);
  expect(registry.ok()).toBeTruthy();
  const types = await registry.json() as Array<{
    name: string;
    core: boolean;
    plugin: string;
    has_validator: boolean;
    presentation?: Record<string, unknown>;
  }>;
  const stars = types.find((type) => type.name === 'stars');
  expect(stars).toMatchObject({
    core: false,
    plugin: PLUGIN_ID,
    has_validator: true,
    presentation: {
      renderer: 'stars',
      base: 'number',
      owner: 'plugin',
      plugin: PLUGIN_ID,
    },
  });

  let columns = await sheetColumns(page.request, pid, sheetId);
  const rating = columns.find((column) => column.name === 'rating');
  expect(rating).toBeTruthy();
  await setColumnType(page.request, pid, rating!.id, 'stars');

  await openProject(page, pid, sheetId);
  columns = await sheetColumns(page.request, pid, sheetId);
  // 'rating' (stars renderer) is a plain non-AI column (now in-place
  // editable — grid-in-place-edit-v1), so the floating icon, not Enter, opens
  // the drawer.
  await openCellDrawer(page, columns, 'rating', 0);

  const drawer = page.getByTestId('row-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId('stars-value')).toContainText('★★★★½');
  await expect(drawer.getByTestId('stars-raw-value')).toHaveText('4.5');

  await page.getByTestId('row-field-rating').hover();
  await drawer.getByTestId('cell-edit-rating').click();
  await drawer.getByTestId('cell-editor-rating').fill('3.5');
  await drawer.getByTestId('cell-save-rating').click();
  await expect(drawer.getByTestId('stars-value')).toContainText('★★★½');
  await expect(drawer.getByTestId('stars-raw-value')).toHaveText('3.5');

  const data = await page.request.get(
    `/api/projects/${pid}/sheets/${sheetId}/data?offset=0&limit=1`,
  );
  expect(data.ok()).toBeTruthy();
  const firstRow = (await data.json()).rows[0] as { id: number; cells: Record<string, unknown> };
  expect(firstRow.cells[String(rating!.id)]).toBe(3.5);

  const invalid = await page.request.post(`/api/projects/${pid}/actions/v1/run`, {
    data: {
      action_id: 'cell.edit',
      scope: { kind: 'project' },
      params: {
        edits: [
          {
            row_id: firstRow.id,
            column_id: rating!.id,
            value: 6,
          },
        ],
      },
      output_names: {},
      idempotency_key: `plugin-stars-invalid-e2e@sha256:${Date.now()}`,
    },
  });
  expect(invalid.status()).toBe(400);
  const invalidBody = await invalid.json() as { errors: Array<{ code: string }> };
  expect(invalidBody.errors[0].code).toBe('column_value_validation_failed');
});
