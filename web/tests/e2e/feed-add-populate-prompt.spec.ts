// On manual feed/source creation -- through the Import workspace's Feed mode
// OR the Sources panel's "Add source" affordance (both reach the exact same
// FeedSourceForm; sources-panel-add-flow-v1 already pins that reuse) -- a
// "Populate feed?" prompt opens immediately, offering the same run the
// Sources panel's own "Poll now" affordance makes (source.poll ->
// api.fetchSource). Run launches it; Not now dismisses with no run.
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
    if (payload.kind === 'source.poll') pollActions.push(payload);
    await route.continue();
  });

  await page.goto(`/p/${pid}`);
  await openImportWorkspace(page);
  await page.getByTestId('import-mode-feed').click();
  await page.getByTestId('source-name').fill(feedName);
  await page.getByTestId('source-url').fill(blockedUrl);
  await page.getByTestId('source-create').click();

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

test('Run launches exactly the manual poll (source.poll on the new source)', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('feed-populate-run'));
  const feedName = uniqueName('Populate-run feed');
  const blockedUrl = 'http://localhost/feed.xml';

  const pollActions: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    if (payload.kind === 'source.poll') pollActions.push(payload);
    await route.continue();
  });

  await page.goto(`/p/${pid}`);
  await openImportWorkspace(page);
  await page.getByTestId('import-mode-feed').click();
  await page.getByTestId('source-name').fill(feedName);
  await page.getByTestId('source-url').fill(blockedUrl);
  await page.getByTestId('source-create').click();

  await expect(page.getByTestId('feed-populate-prompt')).toBeVisible();
  await page.getByTestId('feed-populate-run').click();

  // Same action a Sources-panel "Poll now" click would fire.
  await expect.poll(() => pollActions.length).toBe(1);
  const created = (await sources(page.request, pid)).find((s) => s.name === feedName)!;
  expect(pollActions[0]).toMatchObject({
    schema_version: 'frisket.action.v2',
    kind: 'source.poll',
    capabilities: ['project:write', 'external:source_poll'],
    params: { source_id: created.id },
  });

  // localhost is netguard-blocked, so this real run fails deterministically --
  // same fixture sources.spec.ts's manual-fetch assertion relies on. The
  // prompt surfaces the failure inline (does not silently close) so the
  // dialog stays open with the error visible.
  await expect(page.getByTestId('feed-populate-prompt')).toContainText(/populate failed/i);
  await expect(page.getByTestId('feed-populate-prompt')).toContainText(/blocked|localhost|private/i);
  await expect
    .poll(async () => (await sources(page.request, pid)).find((s) => s.id === created.id)?.last_status)
    .toBe('error');

  // Not now still dismisses from here -- the run already fired; declining
  // just closes.
  await page.getByTestId('feed-populate-dismiss').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();
});

test('the Sources panel\'s Add-source flow reaches the same Populate-feed prompt', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('feed-populate-sources-panel'));
  const feedName = uniqueName('Populate-sources-panel feed');
  const blockedUrl = 'http://localhost/feed.xml';

  await page.goto(`/p/${pid}`);
  await openDiscoverTab(page, 'Sources');
  await page.getByTestId('sources-add-source').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await page.getByTestId('import-mode-feed').click();
  await page.getByTestId('source-name').fill(feedName);
  await page.getByTestId('source-url').fill(blockedUrl);
  await page.getByTestId('source-create').click();

  await expect(page.getByTestId('feed-populate-prompt')).toBeVisible();
  await expect(page.getByTestId('feed-populate-prompt')).toContainText(feedName);
  await page.getByTestId('feed-populate-dismiss').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();

  await expect(page.getByTestId('source-list')).toContainText(feedName);
});
