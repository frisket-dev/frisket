import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  clickCell,
  createProject,
  importCsv,
  openAction,
  openProject,
  revealMenuAction,
  revealRibbonAction,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

// workbench-ia-ribbon-v1 (Workbench IA increment 2): the global chrome bar +
// the Act ribbon ⇄ menu bar, wired to the LIVE action catalog, configure-first.
// Every assertion here binds a clause of the ribbon's acceptance contract.

function countRunPosts(page: Page): { count(): number } {
  let count = 0;
  page.on('request', (request) => {
    if (request.method() === 'POST' && request.url().includes('/actions/v1/run')) {
      count += 1;
    }
  });
  return { count: () => count };
}

async function seedProject(page: Page, csv = 'snippet\nalpha\nbravo\n') {
  const pid = await createProject(page.request, uniqueName('ribbon'));
  const sheetId = await importCsv(page.request, pid, 'seed.csv', csv);
  await openProject(page, pid, sheetId);
  return { pid, sheetId };
}

interface ActLeafSnapshot {
  identity: string;
  label: string;
}

async function orderedActLeaves(container: Locator): Promise<ActLeafSnapshot[]> {
  return container
    .locator('[data-act-item-kind][data-act-item-id]')
    .evaluateAll((elements) =>
      elements.map((element) => {
        const explicitLabel = element.querySelector(
          '.act-ribbon-primary-label, .menu-item-name',
        );
        const secondaryLabel = Array.from(element.children).find(
          (child) => child.tagName === 'SPAN',
        );
        return {
          identity: `${element.getAttribute('data-act-item-kind')}:${element.getAttribute('data-act-item-id')}`,
          label: (explicitLabel?.textContent ?? secondaryLabel?.textContent ?? '').trim(),
        };
      }),
    );
}

test('chrome bar hosts the relocated project menu (no sidebar duplication)', async ({ page }) => {
  await seedProject(page);

  const chrome = page.getByTestId('chrome-bar');
  await expect(chrome).toBeVisible();

  // The project switcher moved out of the sidebar brand into the chrome bar,
  // and the switch-project testid still works from there.
  const switcher = chrome.getByTestId('switch-project');
  await expect(switcher).toBeVisible();
  await switcher.click();
  await expect(page.getByTestId('project-menu')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('project-menu')).toHaveCount(0);

  // The left sidebar is retired entirely (workbench-ia-focus-v1): there is no
  // sidebar to duplicate the project switcher into.
  await expect(page.locator('.sidebar')).toHaveCount(0);
  await expect(page.getByTestId('workbench-region-leftSidebar')).toHaveCount(0);

  // Chrome pill + copilot toggle are present.
  await expect(chrome.getByTestId('chrome-command-pill')).toBeVisible();
  await expect(chrome.getByTestId('chrome-copilot-toggle')).toBeVisible();
});

test('ribbon renders actions from the live catalog, including a no-static-template kind', async ({
  page,
}) => {
  await seedProject(page);

  await expect(page.getByTestId('act-ribbon')).toBeVisible();

  // census_demographics has NO static ActionTemplate — it exists only when the
  // /actions/v1/catalog merge succeeds. Its ribbon reachability proves the
  // surface is catalog-driven without freezing the action's current category.
  await expect(await revealRibbonAction(page, 'enrich.census_demographics')).toBeVisible();
});

test('ribbon Data tab carries import/export command tiles: import dialog + export modals', async ({
  page,
}) => {
  await seedProject(page);

  // Data tab: the "Import data…" command tile opens the SAME import dialog
  // as the Import button (csv/xlsx/json/files/urls), not an action.
  await page.getByTestId('ribbon-tab-data').click();
  const runs = countRunPosts(page);
  await page.getByTestId('ribbon-command-import').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await page.getByTestId('import-workspace-close').click();
  await expect(page.getByTestId('import-workspace-dialog')).toHaveCount(0);

  // Data tab: "Export data" opens the project export modal directly.
  await page.getByTestId('ribbon-tab-data').click();
  await page.getByTestId('ribbon-command-export-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
  await page.getByTestId('export-csv-cancel').click();
  await expect(page.getByTestId('export-data-modal')).toHaveCount(0);

  // Neither command POSTed a run.
  expect(runs.count()).toBe(0);

  // Menu bar parity: Data menu lists the same direct entries.
  await page.getByTestId('ribbon-collapse').click();
  await page.getByTestId('menubar-menu-data').click();
  await page.getByTestId('menu-command-export-csv').click();
  await expect(page.getByTestId('export-data-modal')).toBeVisible();
});

test('clicking a ribbon action opens the ActionPanel configure-first (no run POST)', async ({
  page,
}) => {
  const { pid } = await seedProject(page);
  const runs = countRunPosts(page);

  await openAction(page, 'media.ocr');

  // Configure-first: the panel opens pre-targeted at the OCR action…
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/.*action/media\\.ocr`));
  await expect(page.getByTestId('action-panel')).toBeVisible();
  await expect(page.getByTestId('run-button')).toBeVisible();

  // …and NOTHING ran. The click never POSTs /actions/v1/run.
  await page.waitForTimeout(400);
  expect(runs.count()).toBe(0);
});

test('a selected column pre-binds the configure-first form', async ({ page }) => {
  const { pid, sheetId } = await seedProject(page);
  const columns = await sheetColumns(page.request, pid, sheetId);

  // Select the 'snippet' column by clicking one of its cells.
  await clickCell(page, columns, 'snippet', 0);

  await openAction(page, 'map.extract');

  await expect(page.getByTestId('action-panel')).toBeVisible();
  // The extract form's source column is pre-bound to the selected column.
  await expect(page.getByTestId('text-source-column-select')).toHaveValue('snippet');
});

test('menu-bar mode: dropdown opens, closes on backdrop + Esc, and an item opens the ActionPanel', async ({
  page,
}) => {
  const { pid } = await seedProject(page);

  // Collapse the ribbon → menu bar.
  await page.getByTestId('ribbon-collapse').click();
  await expect(page.getByTestId('act-menubar')).toBeVisible();
  await expect(page.getByTestId('act-ribbon')).toHaveCount(0);

  // Open the Documents menu; an outside click closes it. Retargeted
  // (doctor-popover-fanout-v1, F1 row 14): the dropdown is a native
  // `popover="manual"` top-layer element now, so outside-pointerdown
  // dismissal is structural (useNativePopover) — the hand-rolled
  // `.act-menubar-backdrop` click-catcher this test used to click retired
  // with it. The OUTCOME under test (outside click dismisses) is unchanged;
  // only the implementation-detail testid this asserted through is gone.
  await page.getByTestId('menubar-menu-media').click();
  await expect(page.getByTestId('menubar-dropdown-media')).toBeVisible();
  await page.getByTestId('chrome-bar').click({ position: { x: 1, y: 1 } });
  await expect(page.getByTestId('menubar-dropdown-media')).toHaveCount(0);

  // Esc also closes it.
  await page.getByTestId('menubar-menu-media').click();
  await expect(page.getByTestId('menubar-dropdown-media')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('menubar-dropdown-media')).toHaveCount(0);

  // Native menu-bar gesture while a menu is open: hovering sibling triggers
  // switches the open category without encoding the current category names.
  const menuTriggers = page.getByTestId('act-menubar').locator('.act-menubar-trigger');
  await expect(menuTriggers.first()).toBeVisible();
  expect(await menuTriggers.count()).toBeGreaterThanOrEqual(3);
  await menuTriggers.nth(0).click();
  await expect(menuTriggers.nth(0)).toHaveAttribute('aria-expanded', 'true');
  await menuTriggers.nth(1).hover();
  await expect(menuTriggers.nth(1)).toHaveAttribute('aria-expanded', 'true');
  await expect(menuTriggers.nth(0)).toHaveAttribute('aria-expanded', 'false');
  await menuTriggers.nth(2).hover();
  await expect(menuTriggers.nth(2)).toHaveAttribute('aria-expanded', 'true');
  await expect(menuTriggers.nth(1)).toHaveAttribute('aria-expanded', 'false');
  await page.keyboard.press('Escape');
  await expect(page.locator('.act-menubar-pop')).toHaveCount(0);

  // A menu action item opens the ActionPanel configure-first.
  const runs = countRunPosts(page);
  await (await revealMenuAction(page, 'media.ocr')).click();
  await expect(page).toHaveURL(new RegExp(`/p/${pid}/.*action/media\\.ocr`));
  await expect(page.getByTestId('action-panel')).toBeVisible();
  await expect(page.locator('.act-menubar-pop')).toHaveCount(0);
  await page.waitForTimeout(300);
  expect(runs.count()).toBe(0);
});

test('contextual categories are keyed to column types in both ribbon and compact mode', async ({
  page,
}) => {
  // A sheet without a file column shows no PDF-tools contextual category in
  // either density.
  const pid = await createProject(page.request, uniqueName('ribbon-ctx'));
  const plainSheet = await importCsv(page.request, pid, 'plain.csv', 'note\nhello\nworld\n');
  await openProject(page, pid, plainSheet);
  await expect(page.getByTestId('act-ribbon')).toBeVisible();
  await expect(page.getByTestId('ribbon-tab-ctx-pdf')).toHaveCount(0);
  await page.getByTestId('ribbon-collapse').click();
  await expect(page.getByTestId('act-menubar')).toBeVisible();
  await expect(page.getByTestId('menubar-menu-ctx-pdf')).toHaveCount(0);
  await page.getByTestId('ribbon-expand').click();

  // Give a sheet a file column → the PDF-tools contextual category appears.
  const fileSheet = await importCsv(
    page.request,
    pid,
    'files.csv',
    'doc\nhttps://example.com/a.pdf\nhttps://example.com/b.pdf\n',
  );
  const columns = await sheetColumns(page.request, pid, fileSheet);
  const docColumn = columns.find((column) => column.name === 'doc');
  if (!docColumn) throw new Error('doc column missing');
  await setColumnType(page.request, pid, docColumn.id, 'file');

  await openProject(page, pid, fileSheet);
  await expect(page.getByTestId('act-ribbon')).toBeVisible();
  await expect(page.getByTestId('ribbon-tab-ctx-pdf')).toBeVisible();

  await page.getByTestId('ribbon-tab-ctx-pdf').click();
  const ribbonPdfLeaves = await orderedActLeaves(page.getByTestId('act-ribbon-band'));
  expect(ribbonPdfLeaves.length, 'PDF tools must expose at least one contextual leaf').toBeGreaterThan(0);
  expect(ribbonPdfLeaves.map((leaf) => leaf.identity)).toContain('action:ocr');

  // Compact mode renders the same contextual category and ordered items; it
  // does not discard context just because the surface density changed.
  await page.getByTestId('ribbon-collapse').click();
  await expect(page.getByTestId('menubar-menu-ctx-pdf')).toBeVisible();
  await page.getByTestId('menubar-menu-ctx-pdf').click();
  const compactPdf = page.getByTestId('menubar-dropdown-ctx-pdf');
  await expect(compactPdf).toBeVisible();
  expect(await orderedActLeaves(compactPdf)).toEqual(ribbonPdfLeaves);

  // Exercise the adaptive-tab removal path without pointerdown pre-closing
  // the dropdown: DOM click dispatches only the sheet tab's click handler.
  await page
    .getByTestId(`workbench-mainView-tab-${plainSheet}`)
    .evaluate((element: HTMLElement) => element.click());
  await expect(compactPdf).toHaveCount(0);
  await expect(page.getByTestId('menubar-menu-ctx-pdf')).toHaveCount(0);

  await page.getByTestId('ribbon-expand').click();
  await expect(page.getByTestId('ribbon-tab-analyze')).toHaveAttribute('aria-selected', 'true');
});

test('ribbonMode persists across reload', async ({ page }) => {
  await seedProject(page);

  await page.getByTestId('ribbon-collapse').click();
  await expect(page.getByTestId('act-menubar')).toBeVisible();

  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 20_000 });
  // The collapsed menu-bar density survived the reload (localStorage).
  await expect(page.getByTestId('act-menubar')).toBeVisible();
  await expect(page.getByTestId('act-ribbon')).toHaveCount(0);
});
