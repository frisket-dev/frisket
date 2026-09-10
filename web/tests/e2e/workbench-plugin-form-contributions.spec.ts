import { expect, test, type APIRequestContext } from '@playwright/test';
import { createProject, importCsv, openAction, openDiscoverTab, uniqueName } from './helpers';

// The standalone add-source form retired from the Sources panel (workbench
// entrypoints friction lane — creation now goes through the Import flow), so
// the source-kind form contributions are exercised via the EDIT form: seed a
// source with the v1 source.create action, then open its editor.
async function createRssSource(
  request: APIRequestContext,
  pid: string,
  name: string,
): Promise<number> {
  const res = await request.post(`/api/projects/${pid}/actions/v1/run`, {
    data: {
      action_id: 'source.create',
      scope: { kind: 'project' },
      params: {
        name,
        kind: 'rss',
        url: 'https://example.com/feed.xml',
        schedule: '@hourly',
        enabled: false,
      },
      output_names: {},
      idempotency_key: `e2e-source-create-${name}@sha256:${Date.now().toString(16)}`,
    },
  });
  expect(res.ok()).toBeTruthy();
  const result = await res.json();
  expect(result.status).toBe('completed');
  const sources = await (await request.get(`/api/projects/${pid}/sources`)).json();
  const created = sources.find((source: { name: string }) => source.name === name);
  expect(created).toBeTruthy();
  return created.id;
}

test('source-kind forms render as workbench form contributions', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-workbench-source-form'));
  const sourceName = uniqueName('contribution-feed');
  const sourceId = await createRssSource(page.request, pid, sourceName);
  await page.goto(`/p/${pid}`);

  await openDiscoverTab(page, 'Sources');
  await expect(page.getByTestId('sources-panel')).toBeVisible();
  await page.getByTestId(`source-edit-${sourceId}`).click();

  const rssForm = page.getByTestId('workbench-contribution-frisket-core-source-kind-form-rss');
  await expect(rssForm).toBeVisible();
  await expect(rssForm).toHaveAttribute('data-schema-version', 'frisket.source.kind.form.v1');
  await expect(rssForm).toHaveAttribute('data-contribution-id', 'frisket.core.source_kind_form.rss');
  await expect(rssForm).toHaveAttribute('data-host', 'modalOrPeek');
  await expect(rssForm).toHaveAttribute('data-mode', 'peek');
  await expect(rssForm).toHaveAttribute('data-runtime-component-key', 'core.sources.RssSourceForm');
  await expect(rssForm).toHaveAttribute('data-accepts', 'source.rss');
  await expect(rssForm).toHaveAttribute('data-required-capabilities', /source\.create/);
  await expect(rssForm.getByTestId('source-form')).toBeVisible();

  await page.getByTestId('source-kind').selectOption('api_list_dicts');
  const apiForm = page.getByTestId('workbench-contribution-frisket-core-source-kind-form-api-list-dicts');
  await expect(apiForm).toBeVisible();
  await expect(apiForm).toHaveAttribute('data-schema-version', 'frisket.source.kind.form.v1');
  await expect(apiForm).toHaveAttribute(
    'data-contribution-id',
    'frisket.core.source_kind_form.api_list_dicts',
  );
  await expect(apiForm).toHaveAttribute('data-runtime-component-key', 'core.sources.ApiListDictsSourceForm');
  await expect(apiForm).toHaveAttribute('data-accepts', 'source.api_list_dicts');
  await expect(apiForm.getByTestId('source-api-list-path')).toBeVisible();
});

test('action forms render as workbench action form contributions', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-workbench-action-form'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'note\n"fee was $96 flat"\n');
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // The resident actions panel retired into the Act ribbon + overlay drawer
  // (workbench-ia-action-drawer-v1); the drawer only opens on a loaded sheet.
  // Generated and legacy drawers share the same action-form workbench
  // contribution contract.
  await openAction(page, 'map.summarize');

  const actionForm = page.getByTestId('workbench-contribution-frisket-core-action-form-summarize');
  await expect(actionForm).toBeVisible();
  await expect(actionForm).toHaveAttribute('data-schema-version', 'frisket.action.form.v1');
  await expect(actionForm).toHaveAttribute(
    'data-contribution-id',
    'frisket.core.action_form.summarize',
  );
  await expect(actionForm).toHaveAttribute('data-host', 'modalOrPeek');
  await expect(actionForm).toHaveAttribute('data-mode', 'peek');
  await expect(actionForm).toHaveAttribute(
    'data-runtime-component-key',
    'core.actions.SummarizeForm',
  );
  await expect(actionForm).toHaveAttribute('data-action-kind', 'map.summarize');
  await expect(actionForm).toHaveAttribute('data-form-modes', /run/);
  await expect(actionForm).toHaveAttribute('data-required-capabilities', /action\.run/);
  await expect(actionForm).toHaveAttribute('data-required-permissions', /project\.write/);
  await expect(actionForm.getByTestId('generated-action-form')).toBeVisible();
  await expect(actionForm.getByTestId('action-form-title')).toContainText(/summarize/i);
});
