// The Sources & connections panel needs an add affordance; a management panel
// that cannot add a source is a dead end.
//
// sources.spec.ts already pins that source creation itself lives in the
// Import workspace dialog (ImportCsv.tsx's "Feed" mode: source-name/
// source-url/source-interval/source-create, wired to the source.create v1
// action) and that SourcesPanel must NOT own a second independent creation
// form (`source-add-button` pinned to count 0 there). So this spec proves the
// missing piece only: a discoverable Add affordance INSIDE the Sources panel
// that opens that SAME existing Import dialog (reuse, not reinvent) — not a
// new form, not a new backend flow.

import { expect, test } from '@playwright/test';
import { createProject, openDiscoverTab, openImportWorkspace, uniqueName } from './helpers';

test('the Sources panel has an Add affordance that opens the existing Import flow', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-sources-add-flow'));
  const feedName = uniqueName('Add-flow feed');
  // localhost is netguard-blocked -> rejected synchronously, no network I/O
  // (same deterministic fixture sources.spec.ts uses).
  const blockedUrl = 'http://localhost/feed.xml';

  await page.goto(`/p/${pid}`);
  await openDiscoverTab(page, 'Sources');
  await expect(page.getByTestId('sources-panel')).toBeVisible();

  // Empty state: no sources yet, but the Add affordance is present and does
  // NOT duplicate the creation form locally (sources.spec.ts's invariant).
  await expect(page.getByTestId('sources-empty')).toBeVisible();
  const addButton = page.getByTestId('sources-add-source');
  await expect(addButton).toBeVisible();
  await expect(page.getByTestId('source-add-button')).toHaveCount(0);
  await expect(page.getByTestId('source-form')).toHaveCount(0);

  // Clicking Add opens the SAME Import workspace dialog the ribbon's Import
  // command opens -- reuse, not a bespoke SourcesPanel dialog. It lands
  // directly on the Feed step because Sources manages feeds, not the CSV
  // default that generic Import entry points still start on.
  await addButton.click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await expect(page.getByTestId('import-workspace')).toBeVisible();
  await expect(page.getByTestId('import-mode-feed')).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByTestId('source-name')).toBeVisible();

  // Closing without submitting creates nothing and returns to the panel.
  await page.getByTestId('import-workspace-close').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();
  await expect(page.getByTestId('sources-empty')).toBeVisible();

  // Re-open: still lands on Feed (forced every time, not just the first open)
  // and actually create a source through it -- proves the Add button reaches
  // the real, existing source.create flow.
  await addButton.click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await expect(page.getByTestId('import-mode-feed')).toHaveAttribute('aria-selected', 'true');
  await page.getByTestId('source-name').fill(feedName);
  await page.getByTestId('source-url').fill(blockedUrl);
  await page.getByTestId('source-interval').selectOption('@hourly');
  await page.getByTestId('source-create').click();

  // feed-add-populate-prompt-v1: creation swaps to a "Populate feed?" prompt
  // inline rather than closing outright — decline it here (this spec is
  // about the Add affordance reaching source.create, not about populate).
  await expect(page.getByTestId('feed-populate-prompt')).toBeVisible({ timeout: 10_000 });
  await page.getByTestId('feed-populate-dismiss').click();

  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden({ timeout: 10_000 });
  await expect(page.getByTestId('sources-empty')).toHaveCount(0);
  await expect(page.getByTestId('source-list')).toBeVisible();
  const items = page.getByTestId('source-list').locator('.source-item');
  await expect(items).toContainText(feedName);

  // Non-empty state: the Add affordance stays available for adding more, and
  // still forces Feed (not whatever mode happened to be selected last).
  await expect(addButton).toBeVisible();
  await addButton.click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await expect(page.getByTestId('import-mode-feed')).toHaveAttribute('aria-selected', 'true');
  await page.getByTestId('import-workspace-close').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();
});

test('the generic Import entry point preserves the last-selected mode; Sources Add always forces Feed', async ({
  page,
}) => {
  // sources-panel-add-flow-v1 fix follow-up: only the Sources "Add source"
  // entry point should force Feed. The generic ribbon Import entry point must
  // keep its existing behavior — defaulting to CSV, then remembering
  // whatever mode the user last picked inside the dialog.
  const pid = await createProject(page.request, uniqueName('e2e-sources-add-generic'));
  await page.goto(`/p/${pid}`);

  // The generic ribbon Import command opens on CSV (the existing default) —
  // unaffected by the Sources add-flow's forced Feed mode.
  await openImportWorkspace(page);
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await expect(page.getByTestId('import-mode-csv')).toHaveAttribute('aria-selected', 'true');

  // Switch to Feed manually, then close.
  await page.getByTestId('import-mode-feed').click();
  await page.getByTestId('import-workspace-close').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();

  // Reopening via the generic entry point remembers Feed (the user's last
  // selection) rather than resetting to CSV.
  await openImportWorkspace(page);
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await expect(page.getByTestId('import-mode-feed')).toHaveAttribute('aria-selected', 'true');
  await page.getByTestId('import-workspace-close').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();

  // But the Sources "Add source" entry point still forces Feed regardless —
  // proven trivially true here since Feed is already selected, so switch to
  // CSV first via the generic entry point, then confirm Sources' Add still
  // lands on Feed despite CSV being the last-selected mode.
  await openImportWorkspace(page);
  await page.getByTestId('import-mode-csv').click();
  await page.getByTestId('import-workspace-close').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();

  await openDiscoverTab(page, 'Sources');
  await page.getByTestId('sources-add-source').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await expect(page.getByTestId('import-mode-feed')).toHaveAttribute('aria-selected', 'true');
});
