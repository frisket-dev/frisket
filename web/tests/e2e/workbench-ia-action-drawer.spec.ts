import { expect, test, type Page } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  importCsv,
  openAction,
  openProject,
  selectRow,
  sheetColumns,
  uniqueName,
} from './helpers';

// workbench-ia-action-drawer-v1 (Workbench IA increment 6.5): the ActionPanel
// FORM re-hosted in a fixed-400px right-docked drawer, the split
// Run-all footer, and the column ▾ caret menu.

const CSV = [
  'story,url,risk',
  '"Contract hearing delayed",https://example.com/a,7',
  '"Bridge closure reroutes buses",https://example.com/b,5',
].join('\n');

async function seed(page: Page): Promise<{ pid: string; sheetId: number }> {
  const pid = await createProject(page.request, uniqueName('e2e-action-drawer'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', CSV);
  await openProject(page, pid, sheetId);
  return { pid, sheetId };
}

test('ribbon action reserves a right-side band so the grid ends at the drawer', async ({
  page,
}) => {
  await seed(page);

  // Capture the grid's geometry BEFORE the drawer opens.
  const gridBefore = await page.getByTestId('grid').boundingBox();
  expect(gridBefore).not.toBeNull();

  await openAction(page, 'map.summarize');
  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();

  // Fixed 400px width.
  const drawerBox = await drawer.boundingBox();
  expect(drawerBox).not.toBeNull();
  expect(Math.round(drawerBox!.width)).toBe(400);

  // The grid keeps its left edge and yields its covered width to the drawer.
  const gridAfter = await page.getByTestId('grid').boundingBox();
  expect(gridAfter).not.toBeNull();
  expect(Math.round(gridAfter!.x)).toBe(Math.round(gridBefore!.x));
  expect(gridAfter!.width).toBeLessThan(gridBefore!.width);

  // The two surfaces meet rather than overlap.
  expect(gridAfter!.x + gridAfter!.width).toBeLessThanOrEqual(drawerBox!.x + 2);

  // FULL-HEIGHT (action-drawer-full-height-v1; extended 2026-07-15): the
  // chrome bar (project bar) is never covered — the band starts at its bottom
  // edge; the band now runs to the shell's BOTTOM edge, covering the Monitor
  // dock too. The ribbon IS covered as well (see
  // action-drawer-full-height.spec.ts for the dedicated coverage assertions);
  // this spec only pins the band's outer edges.
  const chromeBar = await page.getByTestId('chrome-bar').boundingBox();
  const monitor = await page.getByTestId('workbench-region-bottomDock').boundingBox();
  expect(drawerBox!.y).toBeGreaterThanOrEqual(chromeBar!.y + chromeBar!.height - 1);
  expect(drawerBox!.y + drawerBox!.height).toBeGreaterThanOrEqual(
    monitor!.y + monitor!.height - 1,
  );
});

test('drawer header shows action name + sheet; × closes and returns focus to the grid', async ({
  page,
}) => {
  await seed(page);
  await openAction(page, 'map.summarize');

  const drawer = page.getByTestId('action-drawer');
  await expect(drawer.getByTestId('action-form-title')).toContainText('Summarize');
  await expect(drawer).toContainText('on stories');
  // No back arrow (single instance, no stack).
  await expect(drawer.getByRole('button', { name: 'Back to actions' })).toHaveCount(0);

  await drawer.getByTestId('action-drawer-close').click();
  await expect(drawer).toBeHidden();
  // Focus returns to the grid.
  await expect(page.getByTestId('grid')).toBeFocused();
});

test('opening a second action replaces the first (single instance)', async ({ page }) => {
  await seed(page);

  await openAction(page, 'map.summarize');
  await expect(page.getByTestId('action-drawer').getByTestId('action-form-title')).toContainText(
    'Summarize',
  );

  await openAction(page, 'map.translate');
  // Still exactly one drawer, now showing translate.
  await expect(page.getByTestId('action-drawer')).toHaveCount(1);
  await expect(page.getByTestId('action-drawer').getByTestId('action-form-title')).toContainText(
    'Translate',
  );
});

test('legacy footer renders scope line, Preview, and a Run-all split with selection + backfill entries', async ({
  page,
}) => {
  await seed(page);
  await openAction(page, 'map.translate');
  const drawer = page.getByTestId('action-drawer');

  // Scope line + Preview + primary Run.
  await expect(drawer.getByTestId('row-scope-summary')).toContainText('All 2 rows will run');
  await expect(drawer.getByTestId('preview-button')).toBeVisible();
  await expect(drawer.getByTestId('run-button')).toBeVisible();

  // Split dropdown: Run selected (disabled with no selection) + Re-run
  // failed/missing (disabled when the target column has no failures/gaps).
  await drawer.getByTestId('run-scope-menu-button').click();
  const menu = drawer.getByTestId('run-scope-menu');
  await expect(menu.getByTestId('row-scope-selected')).toBeDisabled();
  const backfill = menu.getByTestId('row-scope-backfill');
  await expect(backfill).toHaveAttribute('type', 'button');
  await expect(backfill).toHaveAttribute('role', 'menuitem');
  await expect(backfill).toBeDisabled();
  await expect(backfill).toHaveAttribute('title', /Select an existing AI column/);

  // With a selection, Run-selected enables and shows the count. Toggle the
  // menu closed (Escape would close the whole drawer), select a row, reopen.
  await drawer.getByTestId('run-scope-menu-button').click();
  await selectRow(page, 0);
  await drawer.getByTestId('run-scope-menu-button').click();
  await expect(drawer.getByTestId('run-scope-menu').getByTestId('row-scope-selected')).toBeEnabled();
  await expect(
    drawer.getByTestId('run-scope-menu').getByTestId('row-scope-selected'),
  ).toContainText('1');
});

test('Run auto-closes the drawer once the run starts; the job appears in the Monitor', async ({
  page,
}) => {
  const { pid, sheetId } = await seed(page);
  // Use the local `template` action so a REAL job lands in the Monitor (no
  // model call, no cost gate).
  await openAction(page, 'map.template');
  const drawer = page.getByTestId('action-drawer');
  await drawer.getByTestId('field-template').fill('{{story}} — {{risk}}');
  await drawer.getByTestId('field-output-rendered').fill('rendered');

  const runPost = page.waitForRequest(
    (r) => r.url().includes(`/api/projects/${pid}/actions/v1/run`) && r.method() === 'POST',
  );
  await drawer.getByTestId('generated-action-run').click();
  await runPost;

  // action-drawer-autoclose-on-start-v1 (SUPERSEDES the prior "Run keeps the
  // drawer open" contract): the drawer auto-closes on the earliest reliable
  // success signal (the run handle returns / the queued job is accepted) — the
  // new-columns scroll+annotation affordance takes over the feedback role the
  // open drawer used to play.
  await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 15_000 });
  // The Monitor's live run summary still reflects the launched run.
  await expect(page.getByTestId('bottom-dock-run-summary')).toHaveAttribute(
    'data-run-state',
    /running|complete/,
    { timeout: 15_000 },
  );

  // GENERATED columns are legitimate action
  // inputs — the source picker must offer them (a blanket !col.ai filter once
  // hid an AI video column from Extract-video-frames while the caret menu
  // offered the action). Re-open a text action and assert the generated
  // 'rendered' column is pickable, with non-AI columns keeping default rank.
  await expect
    .poll(async () => {
      const cols = await sheetColumns(page.request, pid, sheetId);
      return cols.find((c) => c.name === 'rendered')?.ai_generated ?? false;
    }, { timeout: 15_000 })
    .toBeTruthy();
  await openAction(page, 'map.summarize');
  const sourcePicker = page.getByTestId('text-source-columns');
  await expect(sourcePicker).toBeVisible();
  await sourcePicker.click();
  await expect(page.getByTestId('text-source-columns-menu')
    .getByRole('option', { name: /rendered/ })).toBeVisible();
  // Default stays the first non-AI column rather than silently selecting the
  // newly generated result.
  await expect(sourcePicker.getByRole('button', { name: 'Remove story' })).toBeVisible();
  await expect(sourcePicker.getByRole('button', { name: 'Remove rendered' })).toHaveCount(0);
});

test('column ▾ caret menu offers sort/filter/actions-for-type and opens the drawer pre-bound', async ({
  page,
}) => {
  const { pid, sheetId } = await seed(page);
  const columns = await sheetColumns(page.request, pid, sheetId);

  await clickHeaderMenu(page, columns, 'story');
  const menu = page.getByTestId('grid-column-header-menu');
  await expect(menu).toBeVisible();

  // Sort + Filter + column settings remain reachable.
  await expect(menu.getByTestId('header-menu-sort-asc')).toBeVisible();
  await expect(menu.getByTestId('header-menu-sort-desc')).toBeVisible();
  await expect(menu.getByTestId('header-menu-column-settings')).toBeVisible();

  // The clean-column fast-path is gone; it is now an action that opens the
  // drawer pre-bound to this column.
  await expect(menu.getByTestId('header-menu-clean-column')).toHaveCount(0);
  const cleanAction = menu.getByTestId('header-menu-action-map.clean_column');
  await expect(cleanAction).toBeVisible();
  await cleanAction.click();

  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByTestId('action-form-title')).toContainText(/clean/i);
});

test('caret menu Filter… opens the inline filter row; discovery list is gone from the inspector', async ({
  page,
}) => {
  const { pid, sheetId } = await seed(page);
  const columns = await sheetColumns(page.request, pid, sheetId);

  await clickHeaderMenu(page, columns, 'story');
  await page.getByTestId('grid-column-header-menu').getByTestId('header-menu-filter').click();
  await expect(page.getByTestId('inline-filter-row')).toBeVisible();

  // The resident action-discovery filter list no longer renders anywhere.
  await expect(page.getByTestId('action-discovery-filter')).toHaveCount(0);
  await expect(
    page.getByTestId('workbench-region-rightInspector').getByTestId('action-panel'),
  ).toHaveCount(0);
});

test('an unbreakable long sheet name truncates in the header — no horizontal scroll, × stays reachable', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-drawer-ovf'));
  const longName = `youtube-com-watch-v-EkIb0QMWPbE-list-PL_Aetb7e8ejzekqszBPUi-c5-${'x'.repeat(40)}`;
  const sheetId = await importCsv(page.request, pid, `${longName}.csv`, 'url\nhttps://a.example\n');
  await openProject(page, pid, sheetId);

  await openAction(page, 'media.fetch_url');
  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();

  // The drawer's scroll host must have NO horizontal overflow…
  const overflow = await drawer.evaluate((el) => {
    const host = el.querySelector('.action-drawer-form-host') ?? el;
    return { scrollW: host.scrollWidth, clientW: host.clientWidth };
  });
  expect(overflow.scrollW).toBeLessThanOrEqual(overflow.clientW + 1);

  // …the context span ellipsizes (visibly narrower than its content) and
  // carries the full name in its title…
  const context = drawer.locator('.action-form-context');
  await expect(context).toHaveAttribute('title', new RegExp(longName.slice(0, 24)));
  const truncated = await context.evaluate((el) => el.scrollWidth > el.clientWidth);
  expect(truncated).toBe(true);

  // …the ACTION NAME gets its own line and renders untruncated (two-line
  // header: the context must never squeeze the name)…
  const title = drawer.getByTestId('action-form-title');
  expect(await title.evaluate((el) => el.scrollWidth <= el.clientWidth + 1)).toBe(true);
  const titleBox = await title.boundingBox();
  const contextBox = await context.boundingBox();
  expect(contextBox!.y).toBeGreaterThanOrEqual(titleBox!.y + titleBox!.height - 2);

  // …and the × is inside the drawer's box and clickable without scrolling.
  const closeBox = await page.getByTestId('action-drawer-close').boundingBox();
  const drawerBox = await drawer.boundingBox();
  expect(closeBox).not.toBeNull();
  expect(closeBox!.x + closeBox!.width).toBeLessThanOrEqual(drawerBox!.x + drawerBox!.width + 1);
  await page.getByTestId('action-drawer-close').click();
  await expect(drawer).toHaveCount(0);
});

test('a plain project load never shows the drawer — even with the legacy open flag persisted', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-drawer-stale'));
  const sheetId = await importCsv(page.request, pid, 'a.csv', 'name\nAlpha\n');

  // Plant the resident-era legacy flag BEFORE the app boots (the bug: it
  // stranded an empty drawer stuck on "Loading action…").
  await page.addInitScript(
    ([projectId]) => localStorage.setItem(`frisket:action-panel-open:${projectId}`, '1'),
    [pid],
  );
  await openProject(page, pid, sheetId);

  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('action-drawer')).toHaveCount(0);
  // The legacy key is simply IGNORED — the storage key registration and its
  // one-time sweep retired with zombie-cleanup 2026-07-16 (actionPanelOpen is
  // session-only in memory; nothing reads or writes the key anymore).

  // And a real launch still opens the drawer normally.
  await openAction(page, 'map.summarize');
  await expect(page.getByTestId('action-drawer')).toBeVisible();
});
