import { expect, test } from '@playwright/test';
import { createProject, uniqueName } from './helpers';

type CatalogEntry = {
  kind: string;
  authoring_contract_version: number;
  ui_hints?: {
    internal_shadow?: unknown;
    form?: string;
  };
};

type ActionCatalog = {
  actions: CatalogEntry[];
};

test.use({ screenshot: 'off', trace: 'off' });

test('internal plugin action shadowing is removed from browser-visible product paths', async ({
  page,
}) => {
  const projectId = await createProject(
    page.request,
    uniqueName('plugin-shadow-clean-cutover'),
  );

  const bootstrap = await page.request.post(
    `/api/projects/${projectId}/workbench/internal-plugins/frisket.text_tools/bootstrap`,
  );
  expect(bootstrap.status()).toBe(404);

  const catalogResponse = await page.request.get(
    `/api/projects/${projectId}/actions/v1/catalog`,
  );
  expect(catalogResponse.ok(), await catalogResponse.text()).toBeTruthy();
  const catalog = await catalogResponse.json() as ActionCatalog;
  const cleanDatesEntries = catalog.actions.filter((entry) => entry.kind === 'map.clean_dates');
  expect(cleanDatesEntries).toHaveLength(1);
  expect(cleanDatesEntries[0]).toMatchObject({
    authoring_contract_version: 1,
    ui_hints: { form: 'generated' },
  });
  expect(cleanDatesEntries[0].ui_hints?.internal_shadow).toBeUndefined();
});
