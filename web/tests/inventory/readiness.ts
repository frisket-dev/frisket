import { expect, type Page, type Request } from '@playwright/test';

const catalogs = new WeakMap<Page, Request[]>();

/** Observe the application's requests, so failed or aborted loads cannot be
 * hidden by an unrelated fixture fetch. Full navigations retire the old page. */
export function observeInventoryCatalog(page: Page): void {
  const requests: Request[] = [];
  catalogs.set(page, requests);
  page.on('request', (request) => {
    if (request.isNavigationRequest() && request.frame() === page.mainFrame()
      && request.resourceType() === 'document') requests.length = 0;
    if (request.method() === 'GET'
      && /^\/api\/projects\/[^/]+\/actions\/v1\/catalog$/.test(new URL(request.url()).pathname)) {
      requests.push(request);
    }
  });
}

/** A visible grid or built-in ribbon command does not imply catalog readiness. */
export async function waitForInventoryCatalog(page: Page): Promise<void> {
  const project = new URL(page.url()).pathname.match(/^\/p\/([^/]+)/)?.[1];
  if (!project || await page.getByTestId('workbench-shell').count() === 0) return;
  const endpoint = `/api/projects/${project}/actions/v1/catalog`;
  const requests = catalogs.get(page)?.filter((request) => new URL(request.url()).pathname === endpoint) ?? [];
  expect(requests.length, `Observed application catalog request for ${endpoint}`).toBeGreaterThan(0);
  const before = await page.locator('[data-testid="act-ribbon"] [role="tab"]').allTextContents();
  for (const request of requests) {
    const response = await request.response();
    expect(response, `Catalog request failed: ${request.failure()?.errorText ?? endpoint}`).not.toBeNull();
    expect(response!.ok(), `Catalog HTTP status for ${endpoint}`).toBeTruthy();
    expect(await response!.finished(), `Catalog response completed for ${endpoint}`).toBeNull();
    const payload = await response!.json() as { actions: unknown[] };
    expect(payload.actions.length, `Nonempty action catalog for ${endpoint}`).toBeGreaterThan(0);
  }
  const ribbon = await page.getByTestId('act-ribbon').count() > 0;
  for (const tab of ['analyze', 'tables', 'research']) {
    await expect(page.getByTestId(`${ribbon ? 'ribbon-tab' : 'menubar-menu'}-${tab}`),
      `Catalog-backed ${tab} tab rendered before capture`).toBeVisible();
  }
  console.info(`Catalog ready: ${project}; ${requests.length} successful request(s); tabs before wait: ${before.join(', ')}`);
}

export async function waitForInventoryComparison(page: Page, tab: string): Promise<void> {
  await waitForInventoryCatalog(page);
  const panel = page.getByTestId(`${tab}-tab`);
  await expect(panel).toBeVisible();
  if (tab === 'translate-compare') {
    await expect(panel.getByText('Loading comparison engines…', { exact: true })).toHaveCount(0);
    await expect(panel.getByRole('alert').filter({ hasText: 'Could not load comparison engines' })).toHaveCount(0);
    await expect(panel.getByTestId('translate-compare-billable-note')).toBeVisible();
  }
  if (tab === 'topic-compare') {
    await expect(panel.getByTestId('topic-compare-variant-chip')
      .or(panel.locator('.topic-compare-catalog-message')).first()).toBeVisible();
  }
  await expect(panel.locator('.engine-selector-field-loading')).toHaveCount(0);
  await expect(panel.locator('.engine-selector-field-error')).toHaveCount(0);
}
