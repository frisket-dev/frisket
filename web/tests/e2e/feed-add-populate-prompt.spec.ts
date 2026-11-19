// On manual feed/source creation -- through the Import workspace's Feed mode
// OR the Sources panel's "Add source" affordance (both reach the exact same
// FeedImportForm; sources-panel-add-flow-v1 already pins that reuse) -- a
// "Populate feed?" prompt opens immediately, offering the same run the
// Feeds panel's own "Check now" affordance makes (source.poll ->
// api.fetchFeed). Run queues it; Not now dismisses with no run.
//
// Deterministic + network-free: the source URL is http://localhost/..., which
// the backend SSRF guard (frisket.ops.netguard) rejects synchronously (no DNS,
// no socket) -- same fixture sources.spec.ts uses. A declined ("Not now")
// source therefore stays status=never; a run ("Run") lands a status=error run
// with zero external traffic, proving the exact source.poll action fired.

import { expect, test } from '@playwright/test';
import {
  createProject,
  openDiscoverTab,
  openImportWorkspace,
  uniqueName,
} from './helpers';

interface WireSource {
  id: number;
  name: string;
  last_status: string | null;
}

async function sources(request: import('@playwright/test').APIRequestContext, pid: string): Promise<WireSource[]> {
  const res = await request.get(`/api/projects/${pid}/sources`);
  expect(res.ok()).toBeTruthy();
  return res.json();
}

test('manual feed creation via the import dialog surfaces "Populate feed?" immediately; Not now runs nothing', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('feed-populate-notnow'));
  const feedName = uniqueName('Populate-decline feed');
  const blockedUrl = 'http://localhost/feed.xml';

  const pollActions: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload.action_id === 'source.poll') pollActions.push(payload);
    await route.continue();
  });

  await page.goto(`/p/${pid}`);
  await openImportWorkspace(page);
  await page.getByTestId('import-mode-feed').click();
  await page.getByTestId('feed-name').fill(feedName);
  await page.getByTestId('feed-url').fill(blockedUrl);
  await page.getByTestId('feed-create').click();

  // The prompt opens immediately -- no extra navigation, still inside the
  // same still-open import dialog.
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  const prompt = page.getByTestId('feed-populate-prompt');
  await expect(prompt).toBeVisible();
  await expect(prompt).toContainText('Populate feed?');
  await expect(prompt).toContainText(feedName);
  await expect(page.getByTestId('feed-populate-run')).toBeVisible();
  await expect(page.getByTestId('feed-populate-dismiss')).toBeVisible();

  await page.getByTestId('feed-populate-dismiss').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();

  // No run fired -- the source is created but stays at its never-polled default.
  expect(pollActions).toHaveLength(0);
  await expect
    .poll(async () => (await sources(page.request, pid)).some((s) => s.name === feedName))
    .toBeTruthy();
  const created = (await sources(page.request, pid)).find((s) => s.name === feedName)!;
  expect(created.last_status).toBe('never');
});

test('Run launches exactly the manual poll (source.poll on the new feed)', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('feed-populate-run'));
  const feedName = uniqueName('Populate-run feed');
  const blockedUrl = 'http://localhost/feed.xml';

  const pollActions: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload.action_id === 'source.poll') pollActions.push(payload);
    await route.continue();
  });

  await page.goto(`/p/${pid}`);
  await openImportWorkspace(page);
  await page.getByTestId('import-mode-feed').click();
  await page.getByTestId('feed-name').fill(feedName);
  await page.getByTestId('feed-url').fill(blockedUrl);
  await page.getByTestId('feed-create').click();

  await expect(page.getByTestId('feed-populate-prompt')).toBeVisible();
  await page.getByTestId('feed-populate-run').click();

  // Same action a Feeds-panel "Check now" click would fire.
  await expect.poll(() => pollActions.length).toBe(1);
  const created = (await sources(page.request, pid)).find((s) => s.name === feedName)!;
  expect(pollActions[0]).toMatchObject({
    action_id: 'source.poll',
    scope: { kind: 'project' },
    params: { source: created.id },
  });

  // localhost is netguard-blocked, so this queued poll fails deterministically
  // once the prompt has handed the run off.
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();
  await expect
    .poll(async () => (await sources(page.request, pid)).find((s) => s.id === created.id)?.last_status)
    .toBe('error');
});

test('the Feeds panel\'s Add-feed flow reaches the same Populate-feed prompt', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('feed-populate-sources-panel'));
  const feedName = uniqueName('Populate-sources-panel feed');
  const blockedUrl = 'http://localhost/feed.xml';

  await page.goto(`/p/${pid}`);
  await openDiscoverTab(page, 'Sources');
  await page.getByTestId('feeds-add-feed').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await page.getByTestId('import-mode-feed').click();
  await page.getByTestId('feed-name').fill(feedName);
  await page.getByTestId('feed-url').fill(blockedUrl);
  await page.getByTestId('feed-create').click();

  await expect(page.getByTestId('feed-populate-prompt')).toBeVisible();
  await expect(page.getByTestId('feed-populate-prompt')).toContainText(feedName);
  await page.getByTestId('feed-populate-dismiss').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();

  await expect(page.getByTestId('feed-list')).toContainText(feedName);
});
