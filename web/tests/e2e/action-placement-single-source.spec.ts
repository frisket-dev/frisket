import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openPalette,
  openProject,
  revealRibbonAction,
  uniqueName,
} from './helpers';

// The Act ribbon and compact menu are two renderings of one resolved
// navigation model. Their
// top-level categories and every ordered leaf (action, command, or plugin
// launcher) must match exactly. Named research actions remain reachable, but
// their current category is deliberately not a test contract. Any catalog
// kind the model cannot place falls back to a Misc/Other category (proven here
// with an injected, deliberately-unplaced kind).

declare global {
  interface Window {
    __FRISKET_TEST_UNASSIGNED_ACTION_KIND__?: string;
  }
}

async function seedProject(page: Page): Promise<{ pid: string }> {
  const pid = await createProject(page.request, uniqueName('action-placement'));
  const sheetId = await importCsv(page.request, pid, 'seed.csv', 'snippet\nalpha\nbravo\n');
  await openProject(page, pid, sheetId);
  return { pid };
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

test('ribbon and compact menu expose identical ordered categories and leaves', async ({ page }) => {
  await seedProject(page);

  // The catalog fetch (listActionCatalog) that populates actActionTemplates
  // is async. Wait for a real action leaf before snapshotting the shared
  // model; fixed command leaves can render before the catalog settles.
  await page.getByTestId('ribbon-tab-home').click();
  await expect(page.getByTestId('ribbon-action-map.ask')).toBeVisible();

  const ribbonCategories = await page
    .getByTestId('act-ribbon')
    .locator('.act-ribbon-tab')
    .evaluateAll((elements) =>
      elements.map((element) => ({
        id: (element.getAttribute('data-testid') ?? '').replace(/^ribbon-tab-/, ''),
        label: (element.textContent ?? '').trim(),
      })),
    );
  expect(ribbonCategories.length, 'the shared Act model has no categories').toBeGreaterThan(0);
  expect(
    ribbonCategories.every((category) => category.id.length > 0 && category.label.length > 0),
    'every shared Act category must have an id and visible label',
  ).toBe(true);

  const ribbonLeavesByCategory = new Map<string, ActLeafSnapshot[]>();
  for (const category of ribbonCategories) {
    const trigger = page.getByTestId(`ribbon-tab-${category.id}`);
    await trigger.click();
    await expect(trigger).toHaveAttribute('aria-selected', 'true');
    const leaves = await orderedActLeaves(page.getByTestId('act-ribbon-band'));
    expect(leaves.length, `ribbon category "${category.id}" has no leaves`).toBeGreaterThan(0);
    expect(
      leaves.every((leaf) => leaf.identity.length > 1 && leaf.label.length > 0),
      `ribbon category "${category.id}" has a leaf without identity or label`,
    ).toBe(true);
    ribbonLeavesByCategory.set(category.id, leaves);
  }
  await page.getByTestId('ribbon-collapse').click();
  await expect(page.getByTestId('act-menubar')).toBeVisible();

  const menuCategories = await page
    .getByTestId('act-menubar')
    .locator('.act-menubar-trigger')
    .evaluateAll((elements) =>
      elements.map((element) => ({
        id: (element.getAttribute('data-testid') ?? '').replace(/^menubar-menu-/, ''),
        label: (element.textContent ?? '').trim(),
      })),
    );
  expect(menuCategories, 'compact categories must equal ribbon categories in id, label, and order').toEqual(
    ribbonCategories,
  );

  for (const category of menuCategories) {
    await page.getByTestId(`menubar-menu-${category.id}`).click();
    const dropdown = page.getByTestId(`menubar-dropdown-${category.id}`);
    await expect(dropdown).toBeVisible();
    const menuLeaves = await orderedActLeaves(dropdown);
    expect(menuLeaves.length, `compact category "${category.id}" has no leaves`).toBeGreaterThan(0);
    expect(menuLeaves, `category "${category.id}" must preserve every ribbon leaf and its order`).toEqual(
      ribbonLeavesByCategory.get(category.id),
    );
    await page.keyboard.press('Escape');
    await expect(dropdown).toHaveCount(0);
  }
});

test('research action launchers remain reachable through the shared Act model', async ({
  page,
}) => {
  await seedProject(page);

  await expect(await revealRibbonAction(page, 'research.answer')).toBeVisible();
  await expect(await revealRibbonAction(page, 'research.web_search')).toBeVisible();
});

test('transliterate is discoverable from both Act and Cmd+K', async ({ page }) => {
  const kind = 'frisket.transliterate.transliterate';
  await seedProject(page);

  const ribbonAction = await revealRibbonAction(page, kind);
  await expect(ribbonAction).toContainText(/transliterate/i);

  const palette = await openPalette(page);
  await expect(palette.getByTestId('palette-action-item').first()).toBeVisible();
  await palette.getByTestId('command-palette-input').fill('transliterate');
  await expect(
    palette.locator(`[data-testid="palette-action-item"][data-action-kind="${kind}"]`),
  ).toContainText(/transliterate/i);

  await page.keyboard.press('Escape');
  await (await revealRibbonAction(page, kind)).click();
  await expect(page.getByTestId('generated-action-form')).toBeVisible();
  await expect(page.getByTestId('field-text')).toBeVisible();
});

test('an unassigned launcher kind falls back to the shared Misc/Other category, with one console warning', async ({
  page,
}) => {
  const injectedKind = 'zzz_e2e_unassigned_probe';
  await page.addInitScript((kind) => {
    window.__FRISKET_TEST_UNASSIGNED_ACTION_KIND__ = kind;
  }, injectedKind);

  const warnings: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'warning') warnings.push(message.text());
  });

  await seedProject(page);

  await expect(page.getByTestId('ribbon-tab-misc')).toBeVisible();
  await page.getByTestId('ribbon-tab-misc').click();
  await expect(page.getByTestId(`ribbon-action-${injectedKind}`)).toBeVisible();

  await page.getByTestId('ribbon-collapse').click();
  await expect(page.getByTestId('menubar-menu-misc')).toBeVisible();
  await page.getByTestId('menubar-menu-misc').click();
  await expect(page.getByTestId(`menu-action-${injectedKind}`)).toBeVisible();

  const injectedWarnings = () =>
    warnings.filter((text) => text.includes(injectedKind) && text.includes('ACTION_PLACEMENTS'));
  await expect
    .poll(() => injectedWarnings().length)
    .toBe(1);

  // Re-rendering either density from the shared resolved model must not warn
  // again on every opening.
  await page.keyboard.press('Escape');
  await page.getByTestId('menubar-menu-misc').click();
  await expect(page.getByTestId(`menu-action-${injectedKind}`)).toBeVisible();
  await expect.poll(() => injectedWarnings().length).toBe(1);
});

test('focused categories stay reachable without pushing ribbon controls offscreen', async ({ page }) => {
  await page.setViewportSize({ width: 800, height: 900 });
  await seedProject(page);

  const collapse = page.getByTestId('ribbon-collapse');
  await expect(page.getByTestId('ribbon-tab-location')).toBeAttached();
  const ribbonScrolls = await page.locator('.act-ribbon-tabs').evaluate((tabs) =>
    tabs.scrollWidth > tabs.clientWidth);
  expect(ribbonScrolls).toBe(true);
  await page.getByTestId('ribbon-tab-location').click();
  await expect(page.getByTestId('ribbon-action-enrich.geocode')).toBeVisible();
  const collapseBox = await collapse.boundingBox();
  expect(collapseBox).not.toBeNull();
  expect(collapseBox!.x + collapseBox!.width).toBeLessThanOrEqual(800);

  await collapse.click();
  const expand = page.getByTestId('ribbon-expand');
  const menuScrolls = await page.locator('.act-menubar-menus').evaluate((tabs) =>
    tabs.scrollWidth > tabs.clientWidth);
  expect(menuScrolls).toBe(true);
  await page.getByTestId('menubar-menu-location').click();
  const menu = page.getByTestId('menubar-dropdown-location');
  await expect(menu).toBeVisible();
  const [menuBox, expandBox] = await Promise.all([menu.boundingBox(), expand.boundingBox()]);
  expect(menuBox).not.toBeNull();
  expect(expandBox).not.toBeNull();
  expect(menuBox!.x).toBeGreaterThanOrEqual(0);
  expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(800);
  expect(expandBox!.x + expandBox!.width).toBeLessThanOrEqual(800);
  await page.getByTestId('menu-action-enrich.geocode').click();
  await expect(page.getByTestId('generated-action-form')).toBeVisible();
});

for (const savedTab of ['read', 'location']) {
  test(`catalog loading preserves saved ${savedTab} until tabs can be resolved`, async ({ page }) => {
    const { pid } = await seedProject(page);
    await expect(page.getByTestId('ribbon-action-map.ask')).toBeVisible();
    await page.evaluate(({ pid, savedTab }) => {
      localStorage.setItem(`frisket:ribbon-tab:${pid}`, savedTab);
    }, { pid, savedTab });
    let releaseCatalog!: () => void;
    const catalogGate = new Promise<void>((resolve) => { releaseCatalog = resolve; });
    await page.route('**/actions/v1/catalog', async (route) => {
      await catalogGate;
      await route.continue();
    });
    try {
      await page.reload();
      await expect(page.getByTestId('ribbon-tab-data')).toBeVisible();
      expect(await page.evaluate((pid) => localStorage.getItem(`frisket:ribbon-tab:${pid}`), pid))
        .toBe(savedTab);
    } finally {
      releaseCatalog();
    }
    const expectedTab = savedTab === 'read' ? 'home' : savedTab;
    await expect(page.getByTestId(`ribbon-tab-${expectedTab}`)).toHaveAttribute('aria-selected', 'true');
  });
}
