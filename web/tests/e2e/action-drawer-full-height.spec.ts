import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openAction, openProject, uniqueName } from './helpers';

// The drawer's top edge reaches the top
// chrome's (project bar) bottom edge — the project bar stays visible; the
// ribbon + sheet-tab strip underneath are covered while the drawer is open.
// Closed/collapsed states are unchanged (workbench-ia-action-drawer.spec.ts
// still pins the fixed-400px/no-reflow/band-edges contract; this spec pins
// the NEW coverage + the interactions the coordinator flagged as at risk:
// the sources-panel reveal-over-drawer path, and the ribbon/tab-strip
// clickability under the covering drawer).

async function seed(page: Page): Promise<{ pid: string; sheetId: number }> {
  const pid = await createProject(page.request, uniqueName('e2e-drawer-fullheight'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'story\nAlpha\nBeta\n');
  await openProject(page, pid, sheetId);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });
  return { pid, sheetId };
}

test('open drawer: top edge reaches the top chrome; project bar stays visible', async ({ page }) => {
  await seed(page);
  const chromeBar = await page.getByTestId('chrome-bar').boundingBox();
  expect(chromeBar).not.toBeNull();

  await openAction(page, 'map.summarize');
  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();
  const drawerBox = await drawer.boundingBox();
  expect(drawerBox).not.toBeNull();

  // Drawer top ~= top chrome's bottom (the project bar itself stays above the
  // band, uncovered).
  expect(Math.abs(drawerBox!.y - (chromeBar!.y + chromeBar!.height))).toBeLessThanOrEqual(1);
  await expect(page.getByTestId('chrome-bar')).toBeVisible();
});

test('open drawer: band reaches the shell bottom (over the Monitor dock); Preview/Run footer pins to it', async ({
  page,
}) => {
  await seed(page);
  await openAction(page, 'map.summarize');
  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();
  const drawerBox = await drawer.boundingBox();
  expect(drawerBox).not.toBeNull();

  // The band's bottom edge is the
  // shell's own bottom edge — the Monitor dock is OVERLAID, not avoided.
  const shellBox = await page.getByTestId('workbench-shell').boundingBox();
  expect(
    Math.abs(drawerBox!.y + drawerBox!.height - (shellBox!.y + shellBox!.height)),
  ).toBeLessThanOrEqual(1);

  // The dock really is covered under the drawer's footprint: a hit-test at the
  // dock's right edge resolves inside the drawer.
  const dockBox = await page.getByTestId('workbench-region-bottomDock').boundingBox();
  expect(dockBox).not.toBeNull();
  const dockHit = await page.evaluate(
    ({ x, y }) => {
      const el = document.elementFromPoint(x, y);
      return el?.closest('[data-testid="action-drawer"]') != null;
    },
    { x: dockBox!.x + dockBox!.width - 5, y: dockBox!.y + dockBox!.height / 2 },
  );
  expect(dockHit).toBe(true);

  // The form's Preview/Run footer sits flush at the drawer's real bottom —
  // no dead strip under it when the form is shorter than the drawer.
  const footerBox = await drawer.locator('.action-run-actions').boundingBox();
  expect(footerBox).not.toBeNull();
  expect(
    Math.abs(footerBox!.y + footerBox!.height - (drawerBox!.y + drawerBox!.height)),
  ).toBeLessThanOrEqual(2);
});

test('open drawer: the ribbon + sheet-tab strip are covered — not clickable under it', async ({
  page,
}) => {
  await seed(page);
  await openAction(page, 'map.summarize');
  await expect(page.getByTestId('action-drawer')).toBeVisible();

  // The ribbon's collapse affordance sits at the ribbon row's far right edge —
  // the same horizontal footprint as the drawer's fixed-400px right column —
  // so it is the sharpest probe for "covered, not just visually behind."
  // Playwright's actionability check refuses to click an element another
  // element is painted over; a real click attempt times out.
  await expect(page.getByTestId('ribbon-collapse').click({ timeout: 2_000 })).rejects.toThrow();

  // The sheet-tab strip (Navigate region) is covered too: its hit-test point
  // resolves inside the drawer, not the tab strip.
  const navigateBox = await page.getByTestId('workbench-region-navigate').boundingBox();
  expect(navigateBox).not.toBeNull();
  const hit = await page.evaluate(
    ({ x, y }) => {
      const el = document.elementFromPoint(x, y);
      return el?.closest('[data-testid="action-drawer"]') != null;
    },
    { x: navigateBox!.x + navigateBox!.width - 5, y: navigateBox!.y + navigateBox!.height / 2 },
  );
  expect(hit).toBe(true);
});

test('close: ribbon is clickable again', async ({ page }) => {
  await seed(page);
  await openAction(page, 'map.summarize');
  await expect(page.getByTestId('action-drawer')).toBeVisible();

  await page.getByTestId('action-drawer-close').click();
  await expect(page.getByTestId('action-drawer')).toHaveCount(0);

  // Collapsed/closed behavior is unchanged: the ribbon's own affordances work
  // again once the covering drawer is gone.
  await page.getByTestId('ribbon-collapse').click({ timeout: 2_000 });
  await expect(page.getByTestId('workbench-region-act')).toHaveAttribute('data-ribbon-mode', 'menu');
});

test('the sources-panel reveal-over-drawer interaction still works', async ({ page }) => {
  await seed(page);
  await openAction(page, 'map.summarize');
  await expect(page.getByTestId('action-drawer')).toBeVisible();

  await page.getByTestId('switch-project').click();
  await page.getByTestId('project-sources-open').click();

  // The focused Sources manager wins over the full-height drawer while the
  // draft remains available underneath it.
  await expect(page.getByTestId('sources-connections-dialog')).toBeVisible();
  await expect(page.getByTestId('sources-connections-close')).toBeFocused();
  await expect(page.getByTestId('action-drawer')).toBeVisible();
  await expect(page.getByTestId('sources-panel')).toBeVisible();
});
