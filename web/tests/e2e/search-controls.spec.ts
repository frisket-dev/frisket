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

  const searchRequest = page.waitForRequest((request) => {
    if (request.method() !== 'POST') return false;
    const url = new URL(request.url());
    if (url.pathname !== `/api/projects/${pid}/search`) return false;
    const body = request.postDataJSON() as Record<string, unknown>;
    return body.mode === 'semantic'
      && (body.ranking as { rerank?: string }).rerank === 'on'
      && (body.query as { kind?: string; text?: string }).kind === 'text'
      && (body.query as { text?: string }).text === query;
  });

  // A seeded query returns ranked hits with the new knobs engaged and visible in
  // the wire request.
  await page.getByTestId('command-palette-input').fill(query);
  await searchRequest;
  await expect(page.getByTestId('search-results')).toBeVisible();
  expect(await page.getByTestId('search-hit').count()).toBeGreaterThan(0);

  await page.keyboard.press('Escape');
  await expect(page.getByTestId('workbench-region-commandPalette')).toBeHidden();
});
