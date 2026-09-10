// Review-gallery generator for the plugin NEW HOSTS: a plugin panel hosted as
// a Discover tab and a plugin launcher hosted in the ribbon LIBRARY group.
// Light + dark.
//   npm --prefix web run plugin-contract:e2e -- plugin-hosts-screenshots.spec.ts
import { mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

import { expect, test, type APIRequestContext } from '@playwright/test';
import { createProject, importCsv, openProject, uniqueName } from '../e2e/helpers';
import { SHOT_VIEWPORT, ensureShotsDir, setTheme, shootElement, shotsDir } from '../e2e/screenshotHelpers';

test.use({ viewport: SHOT_VIEWPORT });

const OUT_DIR = shotsDir('workbench-ia');
const THEMES: Array<'light' | 'dark'> = ['light', 'dark'];

test.beforeAll(() => {
  ensureShotsDir(OUT_DIR);
});

// A plugin whose single panel declares BOTH a leftSidebar placement (re-homed
// as a Discover tab) and an activityRail launcher placement (re-homed into the
// ribbon LIBRARY group) — one install exercises both new hosts.
async function writeDualHostPlugin(root: string, pluginId: string, panelId: string) {
  await mkdir(join(root, 'frontend'), { recursive: true });
  await writeFile(
    join(root, 'plugin.json'),
    JSON.stringify({
      schema_version: 'frisket.plugin.v1',
      id: pluginId,
      version: '0.1.0',
      contributes: {
        workbench_views: [],
        workbench_panels: [panelId],
        actions: [], importers: [], operators: [], projections: [], column_types: [], job_handlers: [],
      },
      requires: { capabilities: [], secrets: [] },
      runtime: {
        actions: [], importers: [], operators: [], projections: [], job_handlers: [],
        workbench_components: [
          {
            contribution_id: panelId,
            module_key: `${pluginId}.ui`,
            component_key: `${pluginId}.components.Insights`,
            module_path: 'frontend/plugin.js',
          },
        ],
      },
    }, null, 2),
    'utf-8',
  );
  await writeFile(
    join(root, 'workbench-descriptors.json'),
    JSON.stringify({
      schemaVersion: 'frisket.workbench_descriptor_package.v1',
      descriptors: [
        {
          schemaVersion: 'frisket.workbench.panel.v1',
          id: panelId,
          kind: 'panel',
          title: 'Insights',
          shortTitle: 'Insights',
          icon: 'Boxes',
          ownerPluginId: pluginId,
          componentKey: `${pluginId}.components.Insights`,
          placements: [
            { host: 'leftSidebar', mode: 'panel', slot: 'scope', placementId: `${pluginId}-sidebar`, order: 120 },
            { host: 'activityRail', mode: 'command', slot: 'launcher', placementId: `${pluginId}-rail`, order: 120 },
          ],
          requires: [{ kind: 'hostCapability', id: 'sheet.active' }],
          dataRequirements: [{ kind: 'activeSheet' }],
        },
      ],
    }, null, 2),
    'utf-8',
  );
  await writeFile(
    join(root, 'frontend/plugin.js'),
    `
export const Insights = ({ React, ctx }) =>
  React.createElement('section', {
    'data-testid': 'contract-plugin-panel',
    'data-schema': ctx?.schemaVersion ?? 'missing',
    style: { padding: '14px', display: 'grid', gap: '8px' },
  }, [
    React.createElement('h3', { key: 'h', style: { margin: 0, fontSize: '13px' } }, 'Insights'),
    React.createElement('p', { key: 'p', style: { margin: 0, fontSize: '12px', opacity: 0.75 } },
      'A plugin panel, hosted as a Discover tab. Sheet: ' + (ctx?.sheet?.name ?? '—')),
  ]);
`,
    'utf-8',
  );
}

async function installPlugin(request: APIRequestContext, projectId: string, pluginId: string, root: string) {
  const installed = await request.post(
    `/api/projects/${projectId}/workbench/plugins/${pluginId}/install-local`,
    { data: { source: { kind: 'localPath', value: root }, arbitraryPackageLoadAllowed: false } },
  );
  expect(installed.ok()).toBeTruthy();
  const { receiptId } = (await installed.json()) as { receiptId?: string };
  const activated = await request.post(
    `/api/projects/${projectId}/workbench/plugins/${pluginId}/activate`,
    { data: { receiptId, trustAcknowledged: true, permissionsAccepted: [], arbitraryPackageLoadAllowed: false } },
  );
  expect(activated.ok()).toBeTruthy();
}

for (const theme of THEMES) {
  test(`plugin new-hosts gallery — ${theme}`, async ({ page }, testInfo) => {
    const pluginId = `u${randomUUID().replaceAll('-', '').slice(0, 12)}.host`;
    const panelId = `${pluginId}.panel.insights`;
    const panelSlug = panelId.replace(/[^a-zA-Z0-9]+/g, '-');
    const root = resolve(testInfo.outputPath('plugin-hosts'));
    await writeDualHostPlugin(root, pluginId, panelId);

    const projectId = await createProject(page.request, uniqueName('plugin-hosts'));
    const sheetId = await importCsv(page.request, projectId, 'people.csv', 'name,city\nAda,London\nGrace,Arlington\n');
    await installPlugin(page.request, projectId, pluginId, root);
    await openProject(page, projectId, sheetId);

    // The ribbon Analyze tab (stable internal id `home`) is active by default;
    // the LIBRARY launcher is visible.
    const launcher = page.getByTestId(`ribbon-launcher-${panelSlug}`);
    await expect(launcher).toBeVisible();
    await setTheme(page, theme);
    await shootElement(page, OUT_DIR, 'act-ribbon-band', `plugin-hosts-ribbon-${theme}.png`);

    // Activate the plugin's Discover tab (it may sit in the `»` overflow).
    const tab = page.getByTestId(`discover-tab-${panelSlug}`);
    if (await tab.isVisible()) {
      await tab.click();
    } else {
      await page.getByTestId('discover-tab-overflow').click();
      await page.getByTestId(`discover-tab-menu-${panelSlug}`).click();
    }
    await expect(page.getByTestId('contract-plugin-panel')).toBeVisible();
    await shootElement(page, OUT_DIR, 'discover-panel', `plugin-hosts-discover-${theme}.png`);
  });
}
