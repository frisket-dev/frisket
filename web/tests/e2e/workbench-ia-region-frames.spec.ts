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
// (SettingsSections.tsx), ChromeBarShell (this spec's model — computed-style
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

async function openDockTab(page: Page, placementId: string): Promise<Locator> {
  await page.getByTestId(`bottom-dock-tab-${placementId}`).click();
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-placement-id',
    placementId,
  );
  const frame = page.getByTestId('bottom-dock-panel').locator('.bottom-dock-tab-body').first();
  await expect(frame).toBeVisible();
  return frame;
}

// ---------------------------------------------------------------------------
// (a) R1 — the bottom-dock tab-body frame. One owned wrapper: every dock tab
// body shares ONE frame class with IDENTICAL computed padding, and the old
// three-way split (.workbench-bottom-panel 0 10px / .bottom-dock-contribution
// 6px 0 / …) has collapsed to that single owner.

const PLUGIN_TAB_PLACEMENT_ID = 'region-frames-plugin-tab';

/** Mock the plugin runtime index with ONE bottomDock:tab panel so the parity
 *  assertion covers a PluginDockTabHost tab (the runtime-index route idiom
 *  from workbench-contribution-visibility-controls.spec.ts). */
async function mockPluginDockTab(page: Page, projectId: string): Promise<void> {
  const moduleUrl = `data:text/javascript;charset=utf-8,${encodeURIComponent(`
    export function RegionFramesDockTab({ React }) {
      return React.createElement(
        'div',
        { 'data-testid': 'region-frames-plugin-tab-inner' },
        'Region frames plugin tab'
      );
    }
  `)}`;
  await page.route(`**/api/projects/${projectId}/workbench/plugins`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schemaVersion: 'frisket.workbench_plugin_runtime_index.v1',
        projectId,
        arbitraryPackageLoadAllowed: false,
        receiptScanLimit: 5000,
        skippedInvalidReceipts: 0,
        skippedInvalidManifestRefs: 0,
        loadedPluginCount: 1,
        // WorkbenchPluginRuntimeIndex requires firstParty (the checked-in
        // first-party descriptor package). It carries no plugin tab, so an empty
        // descriptor list keeps the runtime index valid without adding surfaces.
        firstParty: {
          schemaVersion: 'frisket.workbench_descriptor_package.v1',
          descriptors: [],
        },
        plugins: [
          {
            schemaVersion: 'frisket.workbench_plugin_runtime_plugin.v1',
            pluginId: 'frisket.demo.region_frames',
            version: '0.1.0',
            installState: 'enabled',
            activation: 'registryManifestRegistered',
            runtimeSource: 'plugin.load_receipt',
            receiptId: 'receipt_region_frames_dock_tab',
            manifestSha256: 'sha256:region-frames-dock-tab',
            packageSha256: 'sha256:region-frames-dock-package',
            byteCount: 512,
            source: { kind: 'local_file', path: '/plugins/demo_region_frames/plugin.json' },
            contributionSummary: [
              { kind: 'workbench_panel', count: 1, ids: ['frisket.demo.panel.region_frames'] },
            ],
            frontendComponentBindings: [
              {
                schemaVersion: 'frisket.workbench_plugin_frontend_component_binding.v1',
                contributionId: 'frisket.demo.panel.region_frames',
                moduleKey: 'trustedLocal.demoRegionFrames',
                componentKey: 'trustedLocal.demoRegionFrames.RegionFramesDockTab',
                moduleUrl,
              },
            ],
            workbenchDescriptorPackage: {
              schemaVersion: 'frisket.workbench_descriptor_package.v1',
              sourcePath: 'workbench-descriptors.json',
              descriptorCount: 1,
              runtimeOnlyFieldsStripped: [],
            },
            workbenchDescriptorManifests: [
              {
                schemaVersion: 'frisket.workbench.panel.v1',
                id: 'frisket.demo.panel.region_frames',
                kind: 'panel',
                ownerPluginId: 'frisket.demo.region_frames',
                title: 'Region frames tab',
                shortTitle: 'Frames',
                icon: 'PanelBottom',
                placements: [
                  {
                    host: 'bottomDock',
                    mode: 'tab',
                    slot: 'companion.output',
                    order: 40,
                    placementId: PLUGIN_TAB_PLACEMENT_ID,
                  },
                ],
                requires: [],
                dataRequirements: [{ kind: 'activeSheet' }],
              },
            ],
            requires: { capabilities: ['project:read'], secrets: [] },
            arbitraryPackageLoadAllowed: false,
            registryActivated: true,
            installStateSchemaVersion: 'frisket.workbench_plugin_install_state.v1',
            disabledReason: null,
          },
        ],
      }),
    });
  });
}

test('R1: every dock tab body shares one frame class with identical computed padding', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('region-frames-dock'));
  const sheetId = await importCsv(page.request, pid, 'cities.csv', TINY_CSV);
  await mockPluginDockTab(page, pid);
  await openProject(page, pid, sheetId);

  // First-party core-panel tabs, including the tabs that used to carry their
  // own padding variants (plugins and projection), PLUS a PluginDockTabHost
  // plugin tab. If any survives its own padding, this parity breaks.
  const placements = [
    'jobs',
    'errors',
    'history',
    'plugins',
    'projection',
    PLUGIN_TAB_PLACEMENT_ID,
  ];
  const measured: Array<{ placement: string; pt: string; pr: string; pb: string; pl: string }> = [];
  for (const placement of placements) {
    const frame = await openDockTab(page, placement);
    const m = await box(frame);
    measured.push({ placement, pt: m.pt, pr: m.pr, pb: m.pb, pl: m.pl });
  }

  const first = measured[0];
  for (const m of measured) {
    expect(
      `${m.placement}:${m.pt} ${m.pr} ${m.pb} ${m.pl}`,
      `dock tab '${m.placement}' padding must match '${first.placement}'`,
    ).toBe(`${m.placement}:${first.pt} ${first.pr} ${first.pb} ${first.pl}`);
  }

  // The gutter is real (the frame OWNS it) and the host panel no longer does —
  // the three-way split collapsed to one owner.
  expect(first.pt).not.toBe('0px');
  expect(first.pl).not.toBe('0px');
  const host = await box(page.getByTestId('bottom-dock-panel'));
  expect(`${host.pt} ${host.pr} ${host.pb} ${host.pl}`).toBe('0px 0px 0px 0px');
});

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

  // Watches tab: the host gutter is the contract; the watches body adds none.
  await openDiscoverTab(page, 'Watches');
  const bodyW = await box(page.getByTestId('discover-body'));
  expect(`${bodyW.pt} ${bodyW.pr} ${bodyW.pb} ${bodyW.pl}`).toBe(gutter);
  const watches = await box(page.getByTestId('watches-panel'));
  expect(`${watches.pt} ${watches.pr} ${watches.pb} ${watches.pl}`).toBe(zero);

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
// (c) C6 — PanelEmpty primitive. The literal-duplicate muted empty blocks
// collapse to one shared .panel-empty that renders IDENTICALLY (class + computed
// font + color) in a dock tab and a Discover tab.

test('C6: a PanelEmpty renders identically in a dock tab and a Discover tab', async ({ page }) => {
  // A fresh project has no watches and no sources, so both empty states render.
  const pid = await createProject(page.request, uniqueName('region-frames-empty'));
  const sheetId = await importCsv(page.request, pid, 'cities.csv', TINY_CSV);
  await openProject(page, pid, sheetId);

  // Dock: the Watches dock tab's empty state.
  await page.getByTestId('bottom-dock-tab-watches').click();
  const dockEmpty = page.getByTestId('bottom-dock-panel').getByTestId('watches-empty');
  await expect(dockEmpty).toBeVisible();
  await expect(dockEmpty).toHaveClass(/panel-empty/);
  const dockBox = await box(dockEmpty);

  // Discover: the Sources tab's empty state.
  await openDiscoverTab(page, 'Sources');
  const discoverEmpty = page.getByTestId('sources-empty');
  await expect(discoverEmpty).toBeVisible();
  await expect(discoverEmpty).toHaveClass(/panel-empty/);
  const discoverBox = await box(discoverEmpty);

  expect(discoverBox.fontSize).toBe(dockBox.fontSize);
  expect(discoverBox.color).toBe(dockBox.color);
  expect(`${discoverBox.pt} ${discoverBox.pr} ${discoverBox.pb} ${discoverBox.pl}`).toBe(
    `${dockBox.pt} ${dockBox.pr} ${dockBox.pb} ${dockBox.pl}`,
  );
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
  await expect(inspectClose).toHaveClass(/panel-frame-close/);
  const inspectCloseBox = await box(inspectClose);

  // Drawer header (column drawer — the shared Drawer chrome).
  await clickHeaderMenu(page, columns, 'city');
  await page.getByTestId('header-menu-column-settings').click();
  await expect(page.getByTestId('column-drawer')).toBeVisible();
  const drawerHeader = page.getByTestId('column-drawer').locator('.drawer-header');
  await expect(drawerHeader).toHaveClass(/panel-frame-header/);
  const drawerBox = await box(drawerHeader);
  const drawerClose = page.getByTestId('column-drawer').getByRole('button', { name: 'Close drawer' });
  await expect(drawerClose).toHaveClass(/panel-frame-close/);
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
