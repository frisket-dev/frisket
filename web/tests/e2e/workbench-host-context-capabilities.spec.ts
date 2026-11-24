import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openDiscoverTab,
  openFilterSidebar,
  openProject,
  uniqueName,
} from './helpers';

let pid: string;

test.beforeEach(async ({ page }) => {
  pid = await createProject(page.request, uniqueName('host-context-capabilities'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'title,agency\nBudget,Transit\n');
  await openProject(page, pid, sheetId);
});

test('facets proof surface receives typed host context capabilities', async ({ page }) => {
  await openFilterSidebar(page);
  const facets = page.getByTestId('workbench-contribution-frisket-investigative-panel-friendly-filters');
  await expect(facets).toBeVisible();
  await expect(facets).toHaveAttribute('data-host-context', 'WorkbenchHostContextV1');
  await expect(facets).toHaveAttribute('data-host-context-capabilities', /host\.navigation\.openRow/);
  await expect(facets).toHaveAttribute('data-host-context-capabilities', /host\.navigation\.openEvidence/);
  const capabilities = await facets.getAttribute('data-host-context-capabilities');
  expect(capabilities).not.toContain('policy.egressGate');
  expect(capabilities).not.toContain('layout.profile.persist');
  expect(capabilities).not.toContain('layout.profile.reset');
});

test('panels publish the capabilities they require', async ({ page }) => {
  // Each panel is read while it is mounted: the facets in the sheet's Filter
  // sidebar, then Mentions in its Discover tab.
  await openFilterSidebar(page);
  const facets = page.getByTestId('workbench-contribution-frisket-investigative-panel-friendly-filters');
  await expect(facets).toBeVisible();
  await expect(facets).toHaveAttribute('data-required-capabilities', /grid\.filter\.applySpec/);

  await openDiscoverTab(page, 'Mentions');
  const mentions = page.getByTestId('workbench-contribution-frisket-investigative-panel-mentions');
  await expect(mentions).toBeVisible();
  await expect(mentions).toHaveAttribute('data-host-context', 'WorkbenchHostContextV1');
  await expect(mentions).toHaveAttribute('data-required-capabilities', /grid\.filter\.applyEntity/);
});
