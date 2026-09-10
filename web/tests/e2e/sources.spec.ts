// Live-sources (RSS) management UI.
//
// Contract testids the SourcesPanel must expose:
//   sources-panel           — the panel container (always open in its Discover tab)
//   source-name             — Import Feed mode name input
//   source-url              — Import Feed mode URL/reference input
//   source-interval         — cadence <select> in the add form (preset values
//                             "" | "@hourly" | "0 */6 * * *" | "@daily")
//   source-create           — submit the Import Feed form
//   source-list             — list of configured sources
//   source-item-<id>        — one source row
//   source-status-<id>      — last-status badge for that source
//   source-fetch-<id>       — "Fetch now" (manual poll) button
//   source-runs-<id>        — that source's poll-history list (status + error)
//   source-sched-<id>       — per-row cadence <select> (edit → PATCH)
//   source-delete-<id>      — delete control (arms, then confirms)
//
// Deterministic + network-free: the source points at http://localhost/... which
// the backend SSRF guard (frisket.ops.netguard) rejects SYNCHRONOUSLY (localhost
// is a blocked host - no DNS, no socket). So the manual source.poll poll
// lands a status=error source_run with zero external traffic. Successful-ingest
// row-landing is covered by tests/engine/test_rss_ingest.py.

import { expect, test } from '@playwright/test';
import { createProject, openDiscoverTab, openImportWorkspace, uniqueName } from './helpers';

interface WireSource {
  id: number;
  name: string;
  url: string | null;
  schedule: string | null;
}

async function sources(request: import('@playwright/test').APIRequestContext, pid: string): Promise<WireSource[]> {
  const res = await request.get(`/api/projects/${pid}/sources`);
  expect(res.ok()).toBeTruthy();
  return res.json();
}

test('an RSS source can be added, polled, edited, and deleted from the UI', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-sources'));
  const feedName = uniqueName('Test feed');
  // localhost is in netguard BLOCKED_HOSTS → rejected before any network I/O.
  const blockedUrl = 'http://localhost/feed.xml';

  await page.goto(`/p/${pid}`);

  // Sources re-homed into the Discover panel (workbench-ia-right-edge-v1).
  await openDiscoverTab(page, 'Sources');

  const contribution = page.getByTestId('workbench-contribution-frisket-core-panel-sources');
  await expect(contribution).toBeVisible();
  await expect(contribution).toHaveAttribute('data-contribution-id', 'frisket.core.panel.sources');
  await expect(contribution).toHaveAttribute('data-host', 'leftSidebar');
  await expect(contribution).toHaveAttribute('data-mode', 'panel');
  await expect(contribution).toHaveAttribute(
    'data-runtime-component-key',
    'core.panels.SourcesPanel',
  );
  await expect(contribution).toHaveAttribute('data-required-capabilities', /source\.list/);
  await expect(contribution).toHaveAttribute('data-required-capabilities', /source\.poll/);
  await expect(contribution).toHaveAttribute(
    'data-required-capabilities',
    /host\.navigation\.openSheet/,
  );
  await expect(page.getByTestId('sources-panel')).toBeVisible();

  const sourceCreateActions: Array<Record<string, unknown>> = [];
  const sourceUpdateActions: Array<Record<string, unknown>> = [];
  const sourceDeleteActions: Array<Record<string, unknown>> = [];
  const sourceFetchActions: Array<Record<string, unknown>> = [];
  let legacyCreateCalled = false;
  let legacyUpdateCalled = false;
  let legacyDeleteCalled = false;
  let legacyFetchCalled = false;
  const sourceDetailQueries: string[] = [];
  await page.route(`**/api/projects/${pid}/sources`, async (route) => {
    if (route.request().method() === 'POST') {
      legacyCreateCalled = true;
      await route.fulfill({
        status: 599,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'legacy source create route should not be called' }),
      });
      return;
    }
    await route.continue();
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload.action_id === 'source.create') sourceCreateActions.push(payload);
    if (payload.action_id === 'source.update') sourceUpdateActions.push(payload);
    if (payload.action_id === 'source.delete') sourceDeleteActions.push(payload);
    if (payload.kind === 'source.poll') sourceFetchActions.push(payload);
    await route.continue();
  });

  // Add a source through the Import flow; SourcesPanel must not own a second
  // independent creation form.
  await expect(page.getByTestId('source-add-button')).toHaveCount(0);
  await openImportWorkspace(page);
  await page.getByTestId('import-mode-feed').click();
  await page.getByTestId('source-name').fill(feedName);
  await page.getByTestId('source-url').fill(blockedUrl);
  await page.getByTestId('source-interval').selectOption('@hourly');
  await page.getByTestId('source-create').click();
  await expect.poll(() => sourceCreateActions.length).toBe(1);
  expect(sourceCreateActions[0]).toMatchObject({
    action_id: 'source.create',
    scope: { kind: 'project' },
    output_names: {},
    params: {
      name: feedName,
      kind: 'rss',
      url: blockedUrl,
      schedule: '@hourly',
    },
  });
  expect(String(sourceCreateActions[0].idempotency_key)).toMatch(/^web-source\.create:/);
  expect(legacyCreateCalled).toBe(false);

  // feed-add-populate-prompt-v1: creation swaps to a "Populate feed?" prompt
  // instead of closing outright. Decline here — this test drives its own
  // explicit manual poll below and must not double-fire one.
  await expect(page.getByTestId('feed-populate-prompt')).toBeVisible();
  await page.getByTestId('feed-populate-dismiss').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();

  // It lands in the list with a status badge (initially "never").
  await expect
    .poll(async () => (await sources(page.request, pid)).some((s) => s.name === feedName))
    .toBeTruthy();
  const created = (await sources(page.request, pid)).find((s) => s.name === feedName)!;
  const id = created.id;
  expect(created.schedule).toBe('@hourly');
  await expect(page.getByTestId(`source-item-${id}`)).toBeVisible();
  await expect(page.getByTestId(`source-status-${id}`)).toHaveText(/never/i);

  await page.route(`**/api/projects/${pid}/sources/${id}/fetch`, async (route) => {
    legacyFetchCalled = true;
    await route.fulfill({
      status: 599,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'legacy source fetch route should not be called' }),
    });
  });
  await page.route((url) => (
    url.pathname === `/api/projects/${pid}/sources/${id}`
  ), async (route) => {
    if (route.request().method() === 'PATCH') {
      legacyUpdateCalled = true;
      await route.fulfill({
        status: 599,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'legacy source update route should not be called' }),
      });
      return;
    }
    if (route.request().method() === 'DELETE') {
      legacyDeleteCalled = true;
      await route.fulfill({
        status: 599,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'legacy source delete route should not be called' }),
      });
      return;
    }
    if (route.request().method() === 'GET') {
      const url = new URL(route.request().url());
      if (url.searchParams.get('runs_limit') === '20') {
        sourceDetailQueries.push(url.search);
      }
    }
    await route.continue();
  });

  // Manual poll against a blocked URL → the failure surfaces in run history.
  await page.getByTestId(`source-fetch-${id}`).click();
  await expect.poll(() => sourceFetchActions.length).toBe(1);
  expect(sourceFetchActions[0]).toMatchObject({
    schema_version: 'frisket.action.v2',
    kind: 'source.poll',
    capabilities: ['project:write', 'external:source_poll'],
    params: { source_id: id },
  });
  expect(String(sourceFetchActions[0].idempotency_key)).toMatch(/^web-source\.poll:/);
  await expect(page.getByTestId(`source-runs-${id}`)).toBeVisible();
  await expect(page.getByTestId('sources-error')).toContainText(/Poll failed:/);
  await expect(page.getByTestId('sources-error')).toContainText(/blocked|localhost|private/i);
  await expect(page.getByTestId(`source-runs-${id}`)).toContainText(/error/i);
  await expect(page.getByTestId(`source-runs-${id}`)).toContainText(/blocked|localhost|private/i);
  await expect(page.getByTestId(`source-status-${id}`)).toHaveText(/error/i);
  await expect.poll(() => sourceDetailQueries.some((query) => (
    query.includes('runs_offset=0') && query.includes('runs_limit=20')
  ))).toBeTruthy();
  expect(legacyFetchCalled).toBe(false);

  // Edit the cadence through source.update.
  await page.getByTestId(`source-sched-${id}`).selectOption('@daily');
  await expect.poll(() => sourceUpdateActions.length).toBe(1);
  expect(sourceUpdateActions[0]).toMatchObject({
    action_id: 'source.update',
    scope: { kind: 'project' },
    output_names: {},
    params: {
      source_id: id,
      patch: { schedule: '@daily' },
    },
  });
  expect(String(sourceUpdateActions[0].idempotency_key)).toMatch(/^web-source\.update:/);
  await expect
    .poll(async () => (await sources(page.request, pid)).find((s) => s.id === id)?.schedule)
    .toBe('@daily');
  expect(legacyUpdateCalled).toBe(false);

  await page.getByLabel(`Enable ${feedName}`).click();
  await expect.poll(() => sourceUpdateActions.length).toBe(2);
  expect(sourceUpdateActions[1]).toMatchObject({
    action_id: 'source.update',
    scope: { kind: 'project' },
    output_names: {},
    params: {
      source_id: id,
      patch: { enabled: false },
    },
  });
  expect(String(sourceUpdateActions[1].idempotency_key)).toMatch(/^web-source\.update:/);
  await expect
    .poll(async () => (await sources(page.request, pid)).find((s) => s.id === id)?.enabled)
    .toBe(false);
  expect(legacyUpdateCalled).toBe(false);

  // Delete (arm, then confirm) through source.delete → it leaves the list.
  await page.getByTestId(`source-delete-${id}`).click();
  await page.getByTestId(`source-delete-${id}`).click();
  await expect.poll(() => sourceDeleteActions.length).toBe(1);
  expect(sourceDeleteActions[0]).toMatchObject({
    action_id: 'source.delete',
    scope: { kind: 'project' },
    output_names: {},
    params: { source_id: id },
  });
  expect(String(sourceDeleteActions[0].idempotency_key)).toMatch(/^web-source\.delete:/);
  await expect(page.getByTestId(`source-item-${id}`)).toHaveCount(0);
  await expect
    .poll(async () => (await sources(page.request, pid)).find((s) => s.id === id))
    .toBeFalsy();
  expect(legacyDeleteCalled).toBe(false);
});
