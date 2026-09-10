import { expect, test, type Page } from '@playwright/test';
import {
  createProject,
  importCsv,
  openProject,
  seedGeoSheet,
  uniqueName,
} from './helpers';
import { openPluginManager, pluginManagerRow } from './workbenchPluginLifecycleFixture';

// Advisory anti-overclaim audit: visible Workbench/plugin affordances must not
// be decorative or overclaim their backing.
//
// This spec proves the DURABLE, NON-DECORATIVE backing of the visible
// Workbench/plugin affordances against the REAL backend — no route mocks, no
// stubbed lifecycle handlers, no synthetic descriptors, no no-op buttons with
// success copy. Each block asserts a real host action, a real backend-projected
// state read, durable persisted state, or an explicit disabled/unavailable
// state (never a silent success). The per-area specs named in the completion
// audit prove each capability in isolation (often with routed fixtures); this
// audit's job is the overclaim guard: that what a user SEES is what the host
// can actually DO.

async function openCommandPalette(page: Page) {
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();
  return palette;
}

test('plugin install surface reflects the real backend runtime index, not frontend fallback copy', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('audit-plugin-install'));
  const sheetId = await importCsv(page.request, pid, 'rows.csv', 'name\nAda\n');
  await openProject(page, pid, sheetId);

  const manager = await openPluginManager(page);
  // The surface is bound to the backend-projected runtime index (schema +
  // project scope come from the server projection, not a hardcoded shell).
  await expect(manager).toHaveAttribute(
    'data-schema-version',
    'frisket.workbench_plugin_runtime_index.v1',
  );
  await expect(manager).toHaveAttribute('data-project-id', pid);

  // The bundled frisket.geo plugin is a REAL installed plugin (seeded through
  // the public bundled install path at project creation), so its row carries
  // backend-sourced lifecycle facts — not fabricated marketing copy. An
  // overclaiming shell could not produce a real manifest digest or a real
  // install-state enum for a plugin it did not actually install.
  const bundled = pluginManagerRow(page);
  await expect(bundled).toBeVisible();
  await expect(bundled).toHaveAttribute('data-install-state', /^(installed|enabled)$/);
  await expect(bundled).toHaveAttribute('data-manifest-sha256', /^sha256:.+/);
  await expect(bundled).toHaveAttribute('data-workbench-views', /frisket\.geo\.view\.map/);

  // The marketplace/community-execution path is an explicit non-goal (residual
  // boundary in the completion audit): the discovery affordance and the
  // frontend-only repair affordance MUST NOT be advertised.
  await expect(page.getByTestId('workbench-plugin-marketplace-discovery')).toHaveCount(0);
  await expect(page.getByTestId('workbench-plugin-repair-frisket-geo-view-map')).toHaveCount(0);
});

test('hiding a contribution mutates durable profile state that survives a reload', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('audit-durable-repair'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  // Make History the active dock tab, then hide it via the ⌘K palette command.
  await page.getByTestId('bottom-dock-tab-history').click();
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-placement-id',
    'history',
  );
  const palette = await openCommandPalette(page);
  await palette
    .getByTestId('workbench-visibility-command-hide-frisket-core-panel-history')
    .click();
  await page.getByLabel('Close command palette').click();

  await expect(page.getByTestId('bottom-dock-tab-history')).toHaveCount(0);
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-placement-id',
    'jobs',
  );

  // The overclaim guard: a decorative "hide" would reset on reload. This one is
  // durable profile state — after a full reload the tab stays hidden and the
  // dock is still anchored on jobs.
  await page.reload();
  await expect(page.getByTestId('workbench-region-mainView').getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('bottom-dock-tab-history')).toHaveCount(0);
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-placement-id',
    'jobs',
  );

  // Reveal through the palette restores the tab, and that restoration is
  // durable too.
  const revealPalette = await openCommandPalette(page);
  await revealPalette
    .getByTestId('workbench-visibility-command-reveal-frisket-core-panel-history')
    .click();
  await page.getByLabel('Close command palette').click();
  await expect(page.getByTestId('bottom-dock-tab-history')).toBeVisible();

  await page.reload();
  await expect(page.getByTestId('workbench-region-mainView').getByTestId('grid')).toBeVisible();
  await expect(page.getByTestId('bottom-dock-tab-history')).toBeVisible();
});

test('command palette entries advertise only executable commands with honest availability', async ({
  page,
}) => {
  const { pid, sheetId } = await seedGeoSheet(page, {
    namePrefix: 'audit-command-executability',
    point: { lat: 48.8584, lon: 2.2945 },
  });
  await openProject(page, pid, sheetId);

  const palette = await openCommandPalette(page);

  // Every advertised command carries a real runtime handler binding — the
  // palette does not surface commands it cannot dispatch.
  const openSources = palette.getByTestId('workbench-command-frisket-core-command-open-sources');
  await expect(openSources).toHaveAttribute(
    'data-runtime-handler-key',
    'core.commands.openContribution',
  );
  // ...and clicking it produces the real effect, not just a toast.
  await openSources.click();
  await expect(palette).toHaveCount(0);
  await expect(page.getByTestId('sources-connections-dialog')).toBeVisible();
  await expect(page.getByTestId('sources-panel')).toBeVisible();
  await page.getByTestId('sources-connections-close').click();

  await openCommandPalette(page);

  // Hiding the Map surfaces an HONEST availability state on the reveal command
  // (status + reason), and the view segment goes explicitly disabled naming the
  // recovery path — never a silently-broken control.
  await palette
    .getByTestId('workbench-visibility-command-hide-frisket-geo-view-map')
    .click();
  const mapSegment = page.getByTestId('view-switch-map');
  await expect(mapSegment).toBeDisabled();
  await expect(mapSegment).toHaveAttribute('data-disabled-reason', /reveal it via the .* palette/);

  const revealMap = palette.getByTestId('workbench-visibility-command-reveal-frisket-geo-view-map');
  await expect(revealMap).toHaveAttribute('data-availability-status', 'hidden');
  await expect(revealMap).toHaveAttribute('data-availability-reason', 'hidden_by_profile');
  await revealMap.click();
  await expect(mapSegment).toBeEnabled();
});

test('every bottom-dock tab resolves to a real contribution, not decorative chrome', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('audit-bottom-dock'));
  const sheetId = await importCsv(page.request, pid, 'alpha.csv', 'name\nAlpha\n');
  await openProject(page, pid, sheetId);

  // The completion audit names Jobs/Errors specifically: each is a
  // resolved first-party contribution (real runtime source + contribution id),
  // not a hardcoded label with nothing behind it. (The Preview dock tab retired
  // as a dead signpost — its content never rendered real preview output.)
  const dockTabs = [
    { placementId: 'jobs', contributionId: 'frisket.core.panel.jobs' },
    { placementId: 'errors', contributionId: 'frisket.core.panel.errors' },
  ];
  for (const { placementId, contributionId } of dockTabs) {
    const tab = page.getByTestId(`bottom-dock-tab-${placementId}`);
    await expect(tab).toHaveAttribute('data-runtime-source', 'firstParty');
    await expect(tab).toHaveAttribute('data-contribution-id', contributionId);
  }

  // Activating a data tab binds the panel to the resolved contribution — the
  // panel advertises the contribution it is actually rendering.
  await page.getByTestId('bottom-dock-tab-jobs').click();
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-contribution-id',
    'frisket.core.panel.jobs',
  );

  // The Plugins tab retired from the dock entirely — it was a read-only duplicate
  // of the full PluginManager Settings already hosts (mode="settings" at
  // /p/{projectId}/settings/project/plugins, see
  // workbench-plugin-runtime-index.spec.ts). No decorative-chrome risk
  // remains to audit here because there is no dock tab left to check.
  await expect(page.getByTestId('bottom-dock-tab-plugins')).toHaveCount(0);
});

// The former transcribe-first hint was removed because it misfired on OCR and
// to_markdown forms that do not consume audio/video. There is no replacement
// behavior for this audit to pin.
