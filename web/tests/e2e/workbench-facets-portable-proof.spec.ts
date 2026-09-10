import { expect, test } from '@playwright/test';
import { createProject, importCsv, openDiscoverTab, openProject, uniqueName } from './helpers';

const TIPS_CSV = [
  'body,status,amount,filed',
  'Contact the desk before sending the follow-up.,open,10,2026-01-01',
  'Second tip landed in the same queue.,open,20,2026-01-15',
  'Large negative amount arrived from a correction.,open,-1234567.89,2026-01-20',
  'Board heard the item and closed it.,closed,50,2026-02-01',
  'Escalated after the deadline slipped.,closed-late,75,2026-03-01',
  ...Array.from({ length: 22 }, (_, index) =>
    `Synthetic category ${index + 1}.,status-${String(index + 1).padStart(2, '0')},${100 + index},2026-04-${String(index + 1).padStart(2, '0')}`),
].join('\n');

// workbench-ia-right-edge-v1 (increment 4): FriendlyFiltersPanel re-homed from the
// left sidebar into the Discover panel's Facets tab (its single home now — the
// earlier leftSidebar+rightInspector two-host "portable" duplication is retired
// with the region contract). What this test proves is portability: the panel is
// mounted through the contribution system (data-host reflects its declared
// leftSidebar placement, the host context is the real WorkbenchHostContextV1)
// and its interactions — pre-populated facets, server-side high-cardinality
// search, combined filters, and range controls — all work from that host.
test('FriendlyFiltersPanel renders and operates in the Discover panel', async ({ page, request }) => {
  const pid = await createProject(request, uniqueName('e2e-portable-facets'));
  const sheetId = await importCsv(request, pid, 'tips.csv', TIPS_CSV);
  await openProject(page, pid, sheetId);

  const contributionTestId = 'workbench-contribution-frisket-investigative-panel-friendly-filters';
  await openDiscoverTab(page, 'Facets');
  await expect(page.getByTestId(contributionTestId)).toHaveCount(1);

  const discover = page.getByTestId('discover-panel');
  const facets = discover.getByTestId(contributionTestId);
  await expect(facets).toBeVisible();
  await expect(facets).toHaveAttribute(
    'data-contribution-id',
    'frisket.investigative.panel.friendly_filters',
  );
  await expect(facets).toHaveAttribute('data-host', 'leftSidebar');
  await expect(facets).toHaveAttribute('data-slot', 'scope');
  await expect(facets).toHaveAttribute('data-host-context', 'WorkbenchHostContextV1');

  // No second collapse level: the Facets tab already labels this panel, so
  // its body renders open as soon as the tab mounts (no toggle to click).
  await expect(facets.getByTestId('friendly-filters-panel')).toBeVisible();

  const valuesRequest = (expectedSearch: string | null) =>
    page.waitForRequest((req) => {
      if (req.method() !== 'POST') return false;
      if (new URL(req.url()).pathname !== `/api/projects/${pid}/column-values/v1/preview`) {
        return false;
      }
      const body = req.postDataJSON() as { input_column?: string; search?: string };
      return body.input_column === 'status' && (body.search ?? null) === expectedSearch;
    });

  await expect(facets.getByTestId('facet-check-status-open')).toBeVisible();
  await expect(facets.getByTestId('friendly-facet-status')).toContainText('25 values');

  // Keep the target facet at a stable height while filtered counts refresh in
  // the facets above it; this isolates the active-summary insertion regression.
  await facets.getByTestId('friendly-facet-body').locator('.friendly-facet-header').click();

  // The search box searches the SERVER: 'closed-late' is proven to come back
  // from a fresh query carrying `search`, not from narrowing the loaded page.
  const searchedValues = valuesRequest('late');
  await facets.getByTestId('facet-search-status').fill('late');
  await searchedValues;
  await expect(facets.getByTestId('friendly-facet-status')).toContainText('closed-late');

  // Checking a value filters the open grid; the facet-specific chip clears it.
  const closedLate = facets.getByTestId('facet-check-status-closed-late');
  const targetBeforeFilter = await closedLate.boundingBox();
  expect(targetBeforeFilter).not.toBeNull();
  await closedLate.check();
  await expect(facets.getByTestId('facets-active-filter')).toContainText('status eq closed-late');

  // The active-filter summary is below the facet stack, so inserting its first
  // row cannot move the checkbox out from under the pointer.
  const targetAfterFilter = await closedLate.boundingBox();
  expect(targetAfterFilter).toEqual(targetBeforeFilter);

  await page.mouse.click(
    targetBeforeFilter!.x + targetBeforeFilter!.width / 2,
    targetBeforeFilter!.y + targetBeforeFilter!.height / 2,
  );
  await expect(closedLate).not.toBeChecked();
  await expect(facets.getByTestId('facets-active-filter')).toHaveCount(0);

  await closedLate.check();
  await expect(facets.getByTestId('facets-active-filter')).toContainText('status eq closed-late');
  await facets.getByTestId('facets-clear-status').click();
  await expect(facets.getByTestId('facets-active-filter')).toHaveCount(0);

  // Numeric ranges are backed by an exact histogram and use the same filter
  // spec, so they compose with categorical facets instead of replacing them.
  await facets.getByTestId('facet-search-status').fill('');
  await facets.getByTestId('facet-check-status-open').check();
  const amountStart = facets.getByTestId('facet-range-start-amount');
  const amountEnd = facets.getByTestId('facet-range-end-amount');
  const groupedDecimalResponse = page.waitForResponse((response) => {
    if (response.request().method() !== 'GET') return false;
    const url = new URL(response.url());
    if (!url.pathname.endsWith(`/projects/${pid}/sheets/${sheetId}/data`)) return false;
    const raw = url.searchParams.get('filter');
    if (!raw) return false;
    const filter = JSON.parse(raw) as Record<string, { between?: { start: string; end: string } }>;
    return filter.amount?.between?.start === '-1000000.25';
  });
  await amountStart.fill('-1,000,000.25');
  await groupedDecimalResponse;
  await expect(amountStart).toHaveValue('-1,000,000.25');

  await amountStart.fill('15');
  const finalRangeResponse = page.waitForResponse((response) => {
    if (response.request().method() !== 'GET') return false;
    const url = new URL(response.url());
    if (!url.pathname.endsWith(`/projects/${pid}/sheets/${sheetId}/data`)) return false;
    const raw = url.searchParams.get('filter');
    if (!raw) return false;
    const filter = JSON.parse(raw) as Record<string, { between?: { start: string; end: string } }>;
    return filter.amount?.between?.start === '15' && filter.amount.between.end === '60';
  });
  await amountEnd.fill('60');
  await finalRangeResponse;
  await expect(facets.getByTestId('facets-active-filter')).toHaveCount(2);
  await expect(page.getByTestId('active-grid-filter')).toContainText('status eq open');
  await expect(page.getByTestId('active-grid-filter')).toContainText('amount between 15 and 60');
});
