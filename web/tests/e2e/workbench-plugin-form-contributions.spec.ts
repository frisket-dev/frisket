import { expect, test, type APIRequestContext } from '@playwright/test';
import { createProject, openDiscoverTab, uniqueName } from './helpers';

// The standalone add-source form retired from the Sources panel (workbench
// entrypoints friction lane — creation now goes through the Import flow), so
// the source-kind form contributions are exercised via the EDIT form: seed a
// source with the v1 feed.create action, then open its editor.
async function createRssSource(
  request: APIRequestContext,
  pid: string,
  name: string,
): Promise<number> {
  const res = await request.post(`/api/projects/${pid}/actions/v1/run`, {
    data: {
      action_id: 'feed.create',
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
  const sources = await (await request.get(`/api/projects/${pid}/feeds`)).json();
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
  await expect(page.getByTestId('feeds-panel')).toBeVisible();
  await page.getByTestId(`feed-edit-${sourceId}`).click();

  const rssForm = page.getByTestId('workbench-contribution-frisket-core-feed-kind-form-rss');
  await expect(rssForm).toBeVisible();
  await expect(rssForm).toHaveAttribute('data-schema-version', 'frisket.feed.kind.form.v1');
  await expect(rssForm).toHaveAttribute('data-contribution-id', 'frisket.core.feed_kind_form.rss');
  await expect(rssForm).toHaveAttribute('data-host', 'modalOrPeek');
  await expect(rssForm).toHaveAttribute('data-mode', 'peek');
  await expect(rssForm).toHaveAttribute('data-runtime-component-key', 'core.feeds.RssFeedForm');
  await expect(rssForm).toHaveAttribute('data-accepts', 'feed.rss');
  await expect(rssForm).toHaveAttribute('data-required-capabilities', /feed\.create/);
  await expect(rssForm.getByTestId('feed-form')).toBeVisible();

  await page.getByTestId('feed-kind').selectOption('api_list_dicts');
  const apiForm = page.getByTestId('workbench-contribution-frisket-core-feed-kind-form-api-list-dicts');
  await expect(apiForm).toBeVisible();
  await expect(apiForm).toHaveAttribute('data-schema-version', 'frisket.feed.kind.form.v1');
  await expect(apiForm).toHaveAttribute(
    'data-contribution-id',
    'frisket.core.feed_kind_form.api_list_dicts',
  );
  await expect(apiForm).toHaveAttribute('data-runtime-component-key', 'core.feeds.ApiListDictsFeedForm');
  await expect(apiForm).toHaveAttribute('data-accepts', 'feed.api_list_dicts');
  await expect(apiForm.getByTestId('feed-api-list-path')).toBeVisible();
});
