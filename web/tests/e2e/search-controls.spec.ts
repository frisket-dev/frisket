import { expect, test } from '@playwright/test';
import { openPalette, openProject, projectIdByName } from './helpers';

test('search panel exposes semantic mode + rerank and sends those params to search', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  const query = 'listeria';
  await openProject(page, pid);

  // Search folded into the ⌘K palette's SEARCH section (workbench-ia-focus-v1);
  // its mode + rerank knobs live in that section's header.
  await openPalette(page);

  // Mode control switches keyword <-> semantic; a rerank toggle sits beside it.
  await page.getByTestId('search-mode-select').selectOption('semantic');
  await page.getByTestId('search-rerank-toggle').check();

  const searchResponse = page.waitForResponse((response) => {
    const request = response.request();
    if (request.method() !== 'POST') return false;
    const url = new URL(request.url());
    if (url.pathname !== `/api/projects/${pid}/search`) return false;
    const body = request.postDataJSON() as Record<string, unknown>;
    return body.mode === 'semantic'
      && (body.ranking as { rerank?: string }).rerank === 'on'
      && (body.query as { kind?: string; text?: string }).kind === 'text'
      && (body.query as { text?: string }).text === query;
  });

  // The knobs are visible in the wire request. Rerank "on" requires the local
  // reranker, which no setup step installs: without it the palette names the
  // missing model instead of showing unreranked hits.
  await page.getByTestId('command-palette-input').fill(query);
  const reranked = await searchResponse;
  if (reranked.ok()) {
    await expect(page.getByTestId('search-results')).toBeVisible();
  } else {
    expect(reranked.status()).toBe(422);
    await expect(page.getByTestId('workbench-region-commandPalette'))
      .toContainText('reranking model');
  }

  // Semantic mode without a required rerank returns ranked hits.
  await page.getByTestId('search-rerank-toggle').uncheck();
  await expect(page.getByTestId('search-results')).toBeVisible();
  expect(await page.getByTestId('search-hit').count()).toBeGreaterThan(0);

  await page.keyboard.press('Escape');
  await expect(page.getByTestId('workbench-region-commandPalette')).toBeHidden();
});
