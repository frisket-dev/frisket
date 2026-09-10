// UI gate for search mode + rerank. Semantic search and reranking work in the
// backend, but searchProject()
// passes neither mode=semantic nor rerank, so SearchPanel exposes keyword only. This
// spec fails until the panel surfaces the controls and wires them into GET /search.
// The UI must expose both search mode and reranking controls.

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

  const searchResponse = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${pid}/search` &&
      url.searchParams.get('q') === query &&
      url.searchParams.get('mode') === 'semantic' &&
      url.searchParams.get('rerank') === 'on'
    );
  });

  // A seeded query returns ranked hits with the new knobs engaged and visible in
  // the wire request.
  await page.getByTestId('command-palette-input').fill(query);
  expect((await searchResponse).ok()).toBeTruthy();
  await expect(page.getByTestId('search-results')).toBeVisible();
  expect(await page.getByTestId('search-hit').count()).toBeGreaterThan(0);

  await page.keyboard.press('Escape');
  await expect(page.getByTestId('workbench-region-commandPalette')).toBeHidden();
});
