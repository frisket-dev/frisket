import { expect, test, type Locator, type Page } from '@playwright/test';
import {
  clickHeaderMenu,
  createProject,
  importCsv,
  openCellDrawer,
  openDiscoverTab,
  openProject,
  sheetColumns,
  uniqueName,
  type WireColumn,
} from './helpers';

// Region content frames must use consistent wrappers, padding, tables, fonts,
// and headers. Precedents: .discover-body (the gutter-owner contract), SectionFrame
// (SettingsWorkspace.tsx), ChromeBarShell (this spec's model — computed-style
// parity assertions, cf. workbench-ia-shell 'Home and workspace share ONE
// chrome bar').
//
// Each test pins one sub-package by measuring the LIVE computed style of the
// owned frame across regions, so a per-tab padding can never drift back in.
// The dock OCR tab (OcrComparePreviewWorkbenchPanelFrame) and
// ocr-compare-head use a distinct frame and are not asserted here.

const TINY_CSV = 'city\nAlpha\nBravo\nCharlie\n';

function box(el: Locator) {
  return el.evaluate((node) => {
    const s = getComputedStyle(node);
    return {
      pt: s.paddingTop,
      pr: s.paddingRight,
      pb: s.paddingBottom,
      pl: s.paddingLeft,
      fontSize: s.fontSize,
      color: s.color,
      width: s.width,
      height: s.height,
    };
  });
}

// ---------------------------------------------------------------------------
// (b) R2 — Discover gutter re-enforcement. The .discover-body host owns THE one
// gutter; every hosted panel body contributes ZERO own padding (the
// .watches-body / .embeddings-body violations are zeroed at source).

test('R2: Discover hosted panel bodies contribute zero own-padding; the host owns the gutter', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('region-frames-discover'));
  const sheetId = await importCsv(page.request, pid, 'cities.csv', TINY_CSV);
  await openProject(page, pid, sheetId);

  const gutter = '12px 14px 16px 14px';
  const zero = '0px 0px 0px 0px';

  // Alerts tab: the host gutter is the contract; the alerts body adds none.
  await openDiscoverTab(page, 'Watches');
  const bodyW = await box(page.getByTestId('discover-body'));
  expect(`${bodyW.pt} ${bodyW.pr} ${bodyW.pb} ${bodyW.pl}`).toBe(gutter);
  const alerts = await box(page.getByTestId('alerts-panel'));
  expect(`${alerts.pt} ${alerts.pr} ${alerts.pb} ${alerts.pl}`).toBe(zero);

  // Embeddings tab: same contract; the embeddings body adds none.
  await openDiscoverTab(page, 'Embeddings');
  const embeddings = await box(page.getByTestId('embeddings-panel'));
  expect(`${embeddings.pt} ${embeddings.pr} ${embeddings.pb} ${embeddings.pl}`).toBe(zero);

  // The host gutter is stable across tabs (Facets / Sources too).
  await openDiscoverTab(page, 'Sources');
  const bodyS = await box(page.getByTestId('discover-body'));
  expect(`${bodyS.pt} ${bodyS.pr} ${bodyS.pb} ${bodyS.pl}`).toBe(gutter);
});

// ---------------------------------------------------------------------------
// (d) C2 — panel-header consolidation. The adopted headers share ONE header
// class carrying identical computed vertical padding, and the shared close-chip
// gives an identical hit size. (document-reader adopts the same .panel-frame-header
// class in code; its live surface needs a seeded PDF and is covered structurally.)

async function openRowDetail(page: Page, columns: WireColumn[]): Promise<void> {
  // 'city' is a plain non-AI text column (now in-place editable —
  // grid-in-place-edit-v1), so the floating icon, not Enter, opens the drawer.
  await openCellDrawer(page, columns, 'city', 0);
  await expect(page.getByTestId('row-drawer')).toBeVisible();
}

test('C2: adopted panel headers share identical vertical padding and close-chip hit size', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('region-frames-headers'));
  const sheetId = await importCsv(page.request, pid, 'cities.csv', TINY_CSV);
  const columns = await sheetColumns(page.request, pid, sheetId);
  await openProject(page, pid, sheetId);

  // Lineage panel header (dock).
  await page.getByTestId('bottom-dock-tab-lineage').click();
  const lineageHeader = page.getByTestId('bottom-dock-panel').locator('.lineage-panel-header').first();
  await expect(lineageHeader).toHaveClass(/panel-frame-header/);
  const lineageBox = await box(lineageHeader);

  // Inspect-detail header (row detail column).
  await openRowDetail(page, columns);
  const inspectHeader = page.locator('.inspect-detail-header');
  await expect(inspectHeader).toHaveClass(/panel-frame-header/);
  const inspectBox = await box(inspectHeader);
  const inspectClose = page.getByTestId('inspect-detail-close');
  await expect(inspectClose).toHaveClass(/icon-button/);
  await expect(inspectClose).toHaveAttribute('data-icon-size', 'compact');
  const inspectCloseBox = await box(inspectClose);

  // Drawer header (column drawer — the shared Drawer chrome).
  await clickHeaderMenu(page, columns, 'city');
  await page.getByTestId('header-menu-column-settings').click();
  await expect(page.getByTestId('column-drawer')).toBeVisible();
  const drawerHeader = page.getByTestId('column-drawer').locator('.drawer-header');
  await expect(drawerHeader).toHaveClass(/panel-frame-header/);
  const drawerBox = await box(drawerHeader);
  const drawerClose = page.getByTestId('column-drawer').getByRole('button', { name: 'Close drawer' });
  await expect(drawerClose).toHaveClass(/icon-button/);
  await expect(drawerClose).toHaveAttribute('data-icon-size', 'compact');
  const drawerCloseBox = await box(drawerClose);

  // Identical VERTICAL padding across the adopted headers.
  expect(inspectBox.pt).toBe(lineageBox.pt);
  expect(inspectBox.pb).toBe(lineageBox.pb);
  expect(drawerBox.pt).toBe(lineageBox.pt);
  expect(drawerBox.pb).toBe(lineageBox.pb);

  // Identical close-chip hit size.
  expect(inspectCloseBox.width).toBe(drawerCloseBox.width);
  expect(inspectCloseBox.height).toBe(drawerCloseBox.height);
});

// ---------------------------------------------------------------------------
// (e) R3 — SectionFrame adoption. The settings disabled / invalid / error
// sections render via the exported SectionFrame (one shared .settings-section-frame
// class with identical computed padding + the kicker/title/summary anatomy).

async function frameOf(page: Page, testId: string) {
  const frame = page.getByTestId(testId);
  await expect(frame).toBeVisible();
  await expect(frame).toHaveClass(/settings-section-frame/);
  await expect(frame.locator('.settings-section-kicker')).toBeVisible();
  await expect(frame.locator('h1')).toBeVisible();
  return box(frame);
}

test('R3: settings disabled / invalid / normal sections render via one shared SectionFrame', async ({
  page,
}) => {
  await page.goto('/settings/personal/profile');
  const normal = await frameOf(page, 'settings-section-personal-profile');

  await page.goto('/settings/organization/ai-providers');
  const disabled = await frameOf(page, 'settings-disabled-organization-ai-providers');

  await page.goto('/settings/personal/does-not-exist');
  const invalid = await frameOf(page, 'settings-invalid-section');

  // The invalid section is the plain frame — full padding parity.
  expect(`${invalid.pt} ${invalid.pr} ${invalid.pb} ${invalid.pl}`).toBe(
    `${normal.pt} ${normal.pr} ${normal.pb} ${normal.pl}`,
  );
  // The disabled state deliberately keeps its amber left-inset
  // (.settings-section-disabled: border-left + padding-left 14px) ON TOP of the
  // shared frame — everything but that inset matches.
  expect(`${disabled.pt} ${disabled.pr} ${disabled.pb}`).toBe(
    `${normal.pt} ${normal.pr} ${normal.pb}`,
  );
  expect(disabled.pl).toBe('14px');
});
