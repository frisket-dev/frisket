import { expect, test, type Response } from '@playwright/test';
import type { ProjectSearchRequestWire, ProjectSearchResponseWire } from '../../src/api/projectSearch';
import { openPalette, openProject, projectIdByName } from './helpers';

test('search panel exposes semantic mode + rerank and sends those params to search', async ({ page }) => {
  const pid = await projectIdByName(page.request, 'Local stories');
  const query = 'listeria';
  await openProject(page, pid);

  const searchSent = (rerank: 'on' | 'off') => (response: Response) => {
    const request = response.request();
    if (request.method() !== 'POST') return false;
    if (new URL(request.url()).pathname !== `/api/projects/${pid}/search`) return false;
    const body: ProjectSearchRequestWire = request.postDataJSON();
    return body.mode === 'semantic'
      && body.ranking?.rerank === rerank
      && body.query.kind === 'text'
      && body.query.text === query;
  };

  // Search folded into the ⌘K palette's SEARCH section (workbench-ia-focus-v1);
  // its mode + rerank knobs live in that section's header.
  const palette = await openPalette(page);

  // Mode control switches keyword <-> semantic; a rerank toggle sits beside it.
  await page.getByTestId('search-mode-select').selectOption('semantic');
  const semanticResponse = page.waitForResponse(searchSent('off'));
  await page.getByTestId('command-palette-input').fill(query);
  const semantic = await semanticResponse;
  expect(semantic.ok()).toBe(true);

  // Semantic mode without rerank returns ranked hits.
  await expect(page.getByTestId('search-results')).toBeVisible();
  expect(await page.getByTestId('search-hit').count()).toBeGreaterThan(0);

  // The server's answer decides whether Rerank can run. No setup step installs
  // the local reranker model, so where it is missing the toggle is disabled and
  // names what is missing as its tooltip and accessible description.
  const response: ProjectSearchResponseWire = await semantic.json();
  const reason = response.coverage.rerank_unavailable_reason ?? null;
  const toggle = page.getByRole('checkbox', { name: 'Rerank' });
  if (reason !== null) {
    expect(reason).toContain('Rerank');
    await expect(toggle).toBeDisabled();
    await expect(toggle).not.toBeChecked();
    await expect(toggle).toHaveAccessibleDescription(reason);
    await expect(palette.locator('label.palette-search-rerank')).toHaveAttribute('title', reason);
  } else {
    const rerankedResponse = page.waitForResponse(searchSent('on'));
    await toggle.check();
    expect((await rerankedResponse).ok()).toBe(true);
    await expect(page.getByTestId('search-results')).toBeVisible();
  }

  await page.keyboard.press('Escape');
  await expect(page.getByTestId('workbench-region-commandPalette')).toBeHidden();
});
