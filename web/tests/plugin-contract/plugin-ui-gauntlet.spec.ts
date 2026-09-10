import { expect, test, type APIRequestContext } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';

import {
  clickCell,
  clickHeaderMenu,
  createProject,
  editCells,
  importCsv,
  mockBasemapTiles,
  openFriendlyFilterSidebar,
  openAdvancedSortPanel,
  openDiscoverTab,
  openImportWorkspace,
  openProject,
  selectRow,
  setColumnType,
  sheetColumns,
  sheetData,
  uniqueName,
} from '../e2e/helpers';

function suffix(): string {
  return randomUUID().replaceAll('-', '').slice(0, 12);
}

function pluginId(): string {
  return `u${suffix()}.v${suffix()}`;
}

function testIdForContribution(contributionId: string): string {
  return `workbench-contribution-${contributionId.replace(/[^a-zA-Z0-9]+/g, '-')}`;
}

async function writeRuntimeUiPluginPackage({
  root,
  pluginId,
  viewId,
  panelId,
  viewExport,
  panelExport,
  token,
  panelPlacements,
}: {
  root: string;
  pluginId: string;
  viewId: string;
  panelId: string;
  viewExport: string;
  panelExport: string;
  token: string;
  panelPlacements?: Array<Record<string, unknown>>;
}) {
  await mkdir(join(root, 'frontend'), { recursive: true });
  await writeFile(
    join(root, 'plugin.json'),
    JSON.stringify(
      {
        schema_version: 'frisket.plugin.v1',
        id: pluginId,
        version: '0.1.0',
        contributes: {
          workbench_views: [viewId],
          workbench_panels: [panelId],
          actions: [],
          importers: [],
          operators: [],
          projections: [],
          column_types: [],
          job_handlers: [],
        },
        requires: {
          capabilities: [],
          secrets: [],
        },
        runtime: {
          actions: [],
          importers: [],
          operators: [],
          projections: [],
          job_handlers: [],
          workbench_components: [
            {
              contribution_id: viewId,
              module_key: `${pluginId}.ui`,
              component_key: `${pluginId}.components.${viewExport}`,
              module_path: 'frontend/plugin.js',
            },
            {
              contribution_id: panelId,
              module_key: `${pluginId}.ui`,
              component_key: `${pluginId}.components.${panelExport}`,
              module_path: 'frontend/plugin.js',
            },
          ],
        },
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'workbench-descriptors.json'),
    JSON.stringify(
      {
        schemaVersion: 'frisket.workbench_descriptor_package.v1',
        descriptors: [
          {
            schemaVersion: 'frisket.workbench.view.v1',
            id: viewId,
            kind: 'view',
            title: 'Contract Main View',
            ownerPluginId: pluginId,
            componentKey: `${pluginId}.components.${viewExport}`,
            placements: [
              {
                host: 'mainView',
                mode: 'pane',
                slot: 'work.companion',
                placementId: `${pluginId}-main-view`,
                order: 91,
              },
            ],
            requires: [
              { kind: 'hostCapability', id: 'sheet.rows.read' },
              { kind: 'hostCapability', id: 'media.blob.resolve' },
              { kind: 'hostCapability', id: 'host.navigation.openRow' },
            ],
            dataRequirements: [{ kind: 'activeSheet' }],
          },
          {
            schemaVersion: 'frisket.workbench.panel.v1',
            id: panelId,
            kind: 'panel',
            title: 'Contract Panel',
            ownerPluginId: pluginId,
            componentKey: `${pluginId}.components.${panelExport}`,
            placements: panelPlacements ?? [
              {
                host: 'rightInspector',
                mode: 'panel',
                slot: 'inspection',
                placementId: `${pluginId}-right-inspector`,
                order: 91,
              },
            ],
            requires: [
              { kind: 'hostCapability', id: 'sheet.active' },
              { kind: 'hostCapability', id: 'selection.rows' },
              { kind: 'hostCapability', id: 'host.navigation.openRow' },
            ],
            dataRequirements: [{ kind: 'activeSheet' }],
          },
        ],
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'frontend/plugin.js'),
    `
export const ${viewExport} = ({ React, ctx }) => {
  const [state, setState] = React.useState({ status: 'booting', rows: 0, first: '' });
  React.useEffect(() => {
    let active = true;
    if (!ctx || ctx.schemaVersion !== 'frisket.plugin_view_context.v1') {
      setState({ status: 'missing-context', rows: 0, first: '' });
      return () => { active = false; };
    }
    ctx.rows.query({ offset: 0, limit: 2 }).then((page) => {
      if (!active) return;
      setState({
        status: 'ready',
        rows: page.rows.length,
        first: String(page.rows[0]?.id ?? ''),
      });
    }).catch((error) => {
      if (!active) return;
      setState({ status: 'error:' + String(error?.message ?? error), rows: 0, first: '' });
    });
    return () => { active = false; };
  }, [ctx]);
  return React.createElement('section', {
    'data-testid': 'contract-plugin-main-view',
    'data-token': ${JSON.stringify(token)},
    'data-status': state.status,
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-contribution-id': ctx?.contributionId ?? 'missing',
    'data-sheet-name': ctx?.sheet?.name ?? 'missing',
    'data-sheet-row-count': String(ctx?.sheet?.rowCount ?? ''),
    'data-query-row-count': String(state.rows),
    'data-first-row-id': state.first,
    'data-column-names': (ctx?.sheet?.columns ?? []).map((column) => column.name).join(','),
    'data-column-types': (ctx?.sheet?.columns ?? []).map((column) => column.type).join(','),
    'data-has-grid-state': String(ctx?.gridState !== undefined),
    'data-grid-visible-columns': String(ctx?.gridState?.visibleColumnIds?.length ?? -1),
    'data-can-open-row': String(typeof ctx?.navigation?.openRow === 'function'),
    'data-can-resolve-media': String(typeof ctx?.media?.fromCell === 'function'),
  }, React.createElement('button', {
    type: 'button',
    'data-testid': 'contract-plugin-open-first-row',
    onClick: () => state.first && ctx?.navigation?.openRow(state.first),
  }, 'open first row'));
};

export const ${panelExport} = ({ React, ctx }) => {
  return React.createElement('aside', {
    'data-testid': 'contract-plugin-panel',
    'data-token': ${JSON.stringify(token)},
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-contribution-id': ctx?.contributionId ?? 'missing',
    'data-sheet-name': ctx?.sheet?.name ?? 'missing',
    'data-selected-count': String(ctx?.selection?.selectedCount ?? -1),
    'data-active-row-id': String(ctx?.selection?.activeRowId ?? ''),
    'data-grid-filter': JSON.stringify(ctx?.grid?.filter ?? null),
    'data-grid-sort': JSON.stringify(ctx?.grid?.sort ?? null),
    'data-can-open-row': String(typeof ctx?.navigation?.openRow === 'function'),
    'data-dock-active': String(ctx?.dock?.isActiveTab ?? ''),
    'data-can-dock-focus': String(typeof ctx?.dock?.focus === 'function'),
    'data-placement-host': ctx?.placement?.host ?? 'missing',
  }, 'contract plugin panel');
};
`,
    'utf-8',
  );
}

async function writeProjectionViewPluginPackage({
  root,
  pluginId,
  viewId,
  viewExport,
  projectionKind,
  token,
}: {
  root: string;
  pluginId: string;
  viewId: string;
  viewExport: string;
  projectionKind: string;
  token: string;
}) {
  await mkdir(join(root, 'frontend'), { recursive: true });
  await writeFile(
    join(root, 'plugin.json'),
    JSON.stringify(
      {
        schema_version: 'frisket.plugin.v1',
        id: pluginId,
        version: '0.1.0',
        contributes: {
          workbench_views: [viewId],
          workbench_panels: [],
          actions: [],
          importers: [],
          operators: [],
          projections: [],
          column_types: [],
          job_handlers: [],
        },
        requires: {
          capabilities: [],
          secrets: [],
        },
        runtime: {
          actions: [],
          importers: [],
          operators: [],
          projections: [],
          job_handlers: [],
          workbench_components: [
            {
              contribution_id: viewId,
              module_key: `${pluginId}.ui`,
              component_key: `${pluginId}.components.${viewExport}`,
              module_path: 'frontend/plugin.js',
            },
          ],
        },
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'workbench-descriptors.json'),
    JSON.stringify(
      {
        schemaVersion: 'frisket.workbench_descriptor_package.v1',
        descriptors: [
          {
            schemaVersion: 'frisket.workbench.view.v1',
            id: viewId,
            kind: 'view',
            title: 'Contract Projection View',
            ownerPluginId: pluginId,
            componentKey: `${pluginId}.components.${viewExport}`,
            projectionKind,
            placements: [
              {
                host: 'mainView',
                mode: 'pane',
                slot: 'work.companion',
                placementId: `${pluginId}-projection-view`,
                order: 92,
              },
            ],
            requires: [
              { kind: 'hostCapability', id: 'projection.status' },
              { kind: 'hostCapability', id: 'projection.build' },
              { kind: 'hostCapability', id: 'projection.artifact.read' },
              { kind: 'hostCapability', id: 'host.navigation.openRow' },
            ],
            dataRequirements: [
              { kind: 'activeSheet' },
              { kind: 'sheetHasColumnType', columnType: 'date' },
            ],
          },
        ],
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'frontend/plugin.js'),
    `
export const ${viewExport} = ({ React, ctx }) => {
  const [statusSettled, setStatusSettled] = React.useState('pending');
  React.useEffect(() => {
    let active = true;
    if (!ctx || ctx.schemaVersion !== 'frisket.plugin_projection_view_context.v1') {
      setStatusSettled('missing-context');
      return () => { active = false; };
    }
    ctx.projection.status().then(() => {
      if (active) setStatusSettled('resolved');
    }).catch(() => {
      if (active) setStatusSettled('rejected');
    });
    return () => { active = false; };
  }, [ctx]);
  return React.createElement('section', {
    'data-testid': 'contract-plugin-projection-view',
    'data-token': ${JSON.stringify(token)},
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-contribution-id': ctx?.contributionId ?? 'missing',
    'data-projection-kind': ctx?.projection?.kind ?? 'missing',
    'data-target-sheet-id': String(ctx?.projection?.target?.sheetId ?? ''),
    'data-target-date-column-id': String(ctx?.projection?.target?.dateColumnId ?? ''),
    'data-can-status': String(typeof ctx?.projection?.status === 'function'),
    'data-can-build': String(typeof ctx?.projection?.build === 'function'),
    'data-can-read-artifact': String(typeof ctx?.projection?.readArtifact === 'function'),
    'data-status-settled': statusSettled,
    'data-selected-count': String(ctx?.selection?.selectedCount ?? -1),
    'data-selected-row-ids': (ctx?.selection?.selectedRowIds ?? []).join(','),
  }, 'contract projection view');
};
`,
    'utf-8',
  );
}

async function installAndActivatePlugin(
  request: APIRequestContext,
  projectId: string,
  pluginId: string,
  pluginRoot: string,
  options: { permissionsAccepted?: string[]; backendActivate?: boolean } = {},
): Promise<void> {
  const installed = await request.post(`/api/projects/${projectId}/workbench/plugins/${pluginId}/install-local`, {
    data: {
      source: { kind: 'localPath', value: pluginRoot },
      arbitraryPackageLoadAllowed: false,
    },
  });
  expect(installed.ok()).toBeTruthy();
  const installBody = await installed.json() as { receiptId?: string };
  expect(installBody.receiptId).toBeTruthy();

  const activated = await request.post(`/api/projects/${projectId}/workbench/plugins/${pluginId}/activate`, {
    data: {
      receiptId: installBody.receiptId,
      trustAcknowledged: true,
      permissionsAccepted: options.permissionsAccepted ?? [],
      arbitraryPackageLoadAllowed: false,
    },
  });
  expect(activated.ok()).toBeTruthy();

  if (options.backendActivate) {
    const backend = await request.post(
      `/api/projects/${projectId}/workbench/plugins/${pluginId}/backend/activate`,
      {
        data: {
          trustAcknowledged: true,
          arbitraryPackageLoadAllowed: false,
          executableHandlersAllowed: true,
        },
      },
    );
    expect(backend.ok()).toBeTruthy();
  }
}

async function frontendModuleUrl(
  request: APIRequestContext,
  projectId: string,
  pluginId: string,
  contributionId: string,
): Promise<string> {
  const index = await request.get(`/api/projects/${projectId}/workbench/plugins`);
  expect(index.ok()).toBeTruthy();
  const body = await index.json() as {
    plugins: Array<{
      pluginId: string;
      frontendComponentBindings?: Array<{
        contributionId: string;
        moduleUrl?: string;
      }>;
    }>;
  };
  const plugin = body.plugins.find((candidate) => candidate.pluginId === pluginId);
  expect(plugin).toBeTruthy();
  const binding = plugin!.frontendComponentBindings?.find(
    (candidate) => candidate.contributionId === contributionId,
  );
  expect(binding?.moduleUrl).toBeTruthy();
  return binding!.moduleUrl!;
}

async function runtimePluginIds(
  request: APIRequestContext,
  projectId: string,
): Promise<string[]> {
  const index = await request.get(`/api/projects/${projectId}/workbench/plugins`);
  expect(index.ok()).toBeTruthy();
  const body = await index.json() as { plugins: Array<{ pluginId: string }> };
  return body.plugins.map((plugin) => plugin.pluginId);
}

test('frontend module source drift fails closed until plugin reload', async ({
  page,
}, testInfo) => {
  const runSuffix = suffix();
  const generatedPluginId = pluginId();
  const viewId = `${generatedPluginId}.view.${suffix()}`;
  const panelId = `${generatedPluginId}.panel.${suffix()}`;
  const viewExport = `View${runSuffix}`;
  const panelExport = `Panel${runSuffix}`;
  const token = `contract-token-${runSuffix}`;
  const tamperedToken = `tampered-token-${runSuffix}`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-integrity'));
  await writeRuntimeUiPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    viewId,
    panelId,
    viewExport,
    panelExport,
    token,
  });

  const projectId = await createProject(page.request, uniqueName('plugin-contract-ui-integrity'));
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  const moduleUrl = await frontendModuleUrl(page.request, projectId, generatedPluginId, viewId);
  const before = await page.request.get(moduleUrl);
  expect(before.ok()).toBeTruthy();
  expect(await before.text()).toContain(token);

  await writeFile(
    join(pluginRoot, 'frontend/plugin.js'),
    `export const ${viewExport} = () => ${JSON.stringify(tamperedToken)};\n`,
    'utf-8',
  );

  const after = await page.request.get(moduleUrl);
  expect(after.status()).toBe(409);
  const body = await after.text();
  expect(body).toContain('plugin_code_integrity_mismatch');
  expect(body).not.toContain(tamperedToken);
});

test('generated trusted-local view and panel mount with real host contexts', async ({
  page,
}, testInfo) => {
  const runSuffix = suffix();
  const generatedPluginId = pluginId();
  const viewId = `${generatedPluginId}.view.${suffix()}`;
  const panelId = `${generatedPluginId}.panel.${suffix()}`;
  const viewExport = `View${runSuffix}`;
  const panelExport = `Panel${runSuffix}`;
  const token = `contract-token-${runSuffix}`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin'));
  await writeRuntimeUiPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    viewId,
    panelId,
    viewExport,
    panelExport,
    token,
  });

  const projectId = await createProject(page.request, uniqueName('plugin-contract-ui'));
  const otherProjectId = await createProject(page.request, uniqueName('plugin-contract-ui-other'));
  const sheetId = await importCsv(
    page.request,
    projectId,
    'people.csv',
    'name,city\nAda,London\nGrace,Arlington\nLinus,Helsinki\n',
  );
  const data = await sheetData(page.request, projectId, sheetId, 0, 3);
  const firstRowId = String(data.rows[0].id);
  const otherSheetId = await importCsv(
    page.request,
    otherProjectId,
    'people.csv',
    'name,city\nAda,London\nGrace,Arlington\n',
  );

  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  expect(await runtimePluginIds(page.request, projectId)).toContain(generatedPluginId);
  expect(await runtimePluginIds(page.request, otherProjectId)).not.toContain(generatedPluginId);
  const otherModule = await page.request.get(
    `/api/projects/${otherProjectId}/workbench/plugins/${generatedPluginId}/frontend-components/${viewId}/module.js`,
  );
  expect(otherModule.ok()).toBeFalsy();

  await openProject(page, projectId, sheetId);

  const viewFrame = page.getByTestId(testIdForContribution(viewId));
  await expect(viewFrame).toBeVisible();
  await expect(viewFrame).toHaveAttribute(
    'data-runtime-component-key',
    `${generatedPluginId}.components.${viewExport}`,
  );
  await expect(viewFrame).toHaveAttribute('data-plugin-view-status', 'mounted');

  const mainView = page.getByTestId('contract-plugin-main-view');
  await expect(mainView).toBeVisible();
  await expect(mainView).toHaveAttribute('data-token', token);
  await expect(mainView).toHaveAttribute('data-schema', 'frisket.plugin_view_context.v1');
  await expect(mainView).toHaveAttribute('data-contribution-id', viewId);
  await expect(mainView).toHaveAttribute('data-sheet-name', 'people');
  await expect(mainView).toHaveAttribute('data-sheet-row-count', '3');
  await expect(mainView).toHaveAttribute('data-status', 'ready');
  await expect(mainView).toHaveAttribute('data-query-row-count', '2');
  await expect(mainView).toHaveAttribute('data-first-row-id', firstRowId);
  await expect(mainView).toHaveAttribute('data-column-names', 'name,city');
  await expect(mainView).toHaveAttribute('data-column-types', 'text,text');
  await expect(mainView).toHaveAttribute('data-can-open-row', 'true');
  await expect(mainView).toHaveAttribute('data-can-resolve-media', 'true');
  await mainView.getByTestId('contract-plugin-open-first-row').click();
  await expect(page.getByTestId('row-drawer')).toBeVisible();

  const panelFrame = page.getByTestId(testIdForContribution(panelId));
  await expect(panelFrame).toBeVisible();
  await expect(panelFrame).toHaveAttribute(
    'data-runtime-component-key',
    `${generatedPluginId}.components.${panelExport}`,
  );
  await expect(panelFrame).toHaveAttribute('data-plugin-panel-status', 'mounted');

  const panel = page.getByTestId('contract-plugin-panel');
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('data-token', token);
  await expect(panel).toHaveAttribute('data-schema', 'frisket.plugin_panel_context.v1');
  await expect(panel).toHaveAttribute('data-contribution-id', panelId);
  await expect(panel).toHaveAttribute('data-sheet-name', 'people');
  // Post-redesign, host-mediated row open (RowDrawer re-hosted into the
  // Inspect Detail column) SELECTS the opened row — the panel context must
  // reflect that host-driven selection immediately.
  await expect(panel).toHaveAttribute('data-selected-count', '1');
  await expect(panel).toHaveAttribute('data-active-row-id', firstRowId);
  await expect(panel).toHaveAttribute('data-grid-filter', 'null');
  await expect(panel).toHaveAttribute('data-grid-sort', 'null');

  // The 0→1 selection transition above (host-driven open) proves live grid
  // read-state propagation into the panel context; marker-click propagation
  // is separately pinned by 'sdk-built plugin view receives live selection'.
  // Closing the detail column keeps the selection — the context must agree.
  await page.getByLabel('Close drawer').click();
  await expect(page.getByTestId('row-drawer')).not.toBeVisible();
  await expect(panel).toHaveAttribute('data-selected-count', '1');
  await expect(panel).toHaveAttribute('data-can-open-row', 'true');

  // The Plugins dock tab retired entirely — Settings is the sole manager surface now;
  // reach it the way a user does. The manager assertions below are unchanged.
  await page.goto(`/p/${projectId}/settings/project/plugins`);
  const manager = page.getByTestId('plugin-manager');
  await expect(manager).toBeVisible();
  const managerRow = manager.getByTestId('plugin-manager-installed-plugin').filter({
    hasText: generatedPluginId,
  });
  await expect(managerRow).toBeVisible();
  await expect(managerRow).toHaveAttribute('data-plugin-id', generatedPluginId);
  await expect(managerRow).toHaveAttribute('data-install-state', 'enabled');
  await expect(managerRow).toHaveAttribute('data-workbench-views', new RegExp(viewId));
  await expect(managerRow).toHaveAttribute('data-workbench-panels', new RegExp(panelId));
  await expect(managerRow).toHaveAttribute('data-frontend-bindings', new RegExp(viewExport));
  await expect(managerRow).toHaveAttribute('data-manifest-sha256', /^sha256:/);
  await expect(managerRow).toHaveAttribute('data-package-sha256', /^sha256:/);

  await openProject(page, otherProjectId, otherSheetId);
  await expect(page.getByTestId(testIdForContribution(viewId))).toHaveCount(0);
  await expect(page.getByTestId(testIdForContribution(panelId))).toHaveCount(0);
  await expect(page.getByTestId('contract-plugin-main-view')).toHaveCount(0);
  await expect(page.getByTestId('contract-plugin-panel')).toHaveCount(0);
});

test('generated projection view mounts generically from descriptor projectionKind', async ({
  page,
}, testInfo) => {
  const runSuffix = suffix();
  const generatedPluginId = pluginId();
  const viewId = `${generatedPluginId}.view.${suffix()}`;
  const viewExport = `ProjectionView${runSuffix}`;
  const projectionKind = `${generatedPluginId}.projection.${suffix()}`;
  const token = `contract-token-${runSuffix}`;
  const pluginRoot = resolve(testInfo.outputPath('generated-projection-plugin'));
  await writeProjectionViewPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    viewId,
    viewExport,
    projectionKind,
    token,
  });

  const projectId = await createProject(
    page.request,
    uniqueName('plugin-contract-projection'),
  );
  const sheetId = await importCsv(
    page.request,
    projectId,
    'events.csv',
    'name,happened\nFiling,2020-01-02\nHearing,2020-03-04\n',
  );
  const columns = await sheetColumns(page.request, projectId, sheetId);
  const dateColumn = columns.find((column) => column.name === 'happened');
  expect(dateColumn).toBeTruthy();
  await setColumnType(page.request, projectId, dateColumn!.id, 'date');

  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  const viewFrame = page.getByTestId(testIdForContribution(viewId));
  await expect(viewFrame).toBeVisible();
  await expect(viewFrame).toHaveAttribute('data-plugin-view-status', 'mounted');
  await expect(viewFrame).toHaveAttribute(
    'data-plugin-projection-view-context-schema-version',
    'frisket.plugin_projection_view_context.v1',
  );
  await expect(viewFrame).toHaveAttribute('data-plugin-projection-kind', projectionKind);
  await expect(viewFrame).toHaveAttribute('data-plugin-runtime-package-sha256', /^sha256:/);

  const projectionView = page.getByTestId('contract-plugin-projection-view');
  await expect(projectionView).toBeVisible();
  await expect(projectionView).toHaveAttribute('data-token', token);
  await expect(projectionView).toHaveAttribute(
    'data-schema',
    'frisket.plugin_projection_view_context.v1',
  );
  await expect(projectionView).toHaveAttribute('data-contribution-id', viewId);
  await expect(projectionView).toHaveAttribute('data-projection-kind', projectionKind);
  await expect(projectionView).toHaveAttribute('data-target-sheet-id', String(sheetId));
  await expect(projectionView).toHaveAttribute(
    'data-target-date-column-id',
    String(dateColumn!.id),
  );
  await expect(projectionView).toHaveAttribute('data-can-status', 'true');
  await expect(projectionView).toHaveAttribute('data-can-build', 'true');
  await expect(projectionView).toHaveAttribute('data-can-read-artifact', 'true');
  // The context methods are wired through the host into the real projection API;
  // the generated kind has no registered backend projection, so the call settles
  // either way — settling proves the host wiring executes, not backend registration.
  await expect(projectionView).toHaveAttribute('data-status-settled', /^(resolved|rejected)$/);
});

async function writeProjectionDataReadPluginPackage({
  root,
  pluginId,
  viewId,
  viewExport,
  projectionKind,
  token,
  withDataReadCapability,
}: {
  root: string;
  pluginId: string;
  viewId: string;
  viewExport: string;
  projectionKind: string;
  token: string;
  withDataReadCapability: boolean;
}) {
  await mkdir(join(root, 'frontend'), { recursive: true });
  await writeFile(
    join(root, 'plugin.json'),
    JSON.stringify(
      {
        schema_version: 'frisket.plugin.v1',
        id: pluginId,
        version: '0.1.0',
        contributes: {
          workbench_views: [viewId],
          workbench_panels: [],
          actions: [],
          importers: [],
          operators: [],
          // The plugin-owned map-points world
          // (MAP_POINTS_DESCRIPTOR_WORLD_IS_PLUGIN_OWNED, geo-bundled-plugin-v1)
          // refuses point serving without an active role-map_points binding —
          // this fixture owns its own, staying hermetic instead of leaning on
          // the bundled geo plugin's binding.
          projections: [projectionKind],
          column_types: [],
          job_handlers: [],
        },
        requires: {
          capabilities: ['plugin:trusted_local_backend'],
          secrets: [],
        },
        runtime: {
          actions: [],
          importers: [],
          operators: [],
          projections: [
            {
              kind: projectionKind,
              handler_key: `${pluginId}:map_points`,
              handler_api: 'plugin_projection',
              module_path: 'plugin.py',
              execution: { mode: 'runtime_plan', role: 'map_points' },
            },
          ],
          job_handlers: [],
          workbench_components: [
            {
              contribution_id: viewId,
              module_key: `${pluginId}.ui`,
              component_key: `${pluginId}.components.${viewExport}`,
              module_path: 'frontend/plugin.js',
            },
          ],
        },
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'plugin.py'),
    [
      'from __future__ import annotations',
      '',
      'from typing import Any',
      '',
      'from frisket.plugins.sdk import Plugin',
      '',
      'plugin = Plugin.from_toml(__file__)',
      '',
      '',
      '@plugin.projection(',
      '    "map_points",',
      '    handler_key="map_points",',
      '    title="Contract map points projection",',
      ')',
      'async def map_points_projection(',
      '    ctx,',
      '    _rows: list[dict[str, Any]],',
      '    *,',
      '    target: dict[str, Any],',
      '    params: dict[str, Any],',
      '    mode: str = "status",',
      ') -> dict[str, Any]:',
      '    del target',
      '    engine = params.get("pointBackend") or {}',
      '    generation = str(engine.get("generationHash") or "") or None',
      '    if mode == "status":',
      '        return {',
      '            "schemaVersion": "frisket.runtime_projection_status.v1",',
      '            "status": "ready" if generation else "stale",',
      '            "freshness": {',
      '                "state": "fresh" if generation else "stale",',
      '                "generation": generation,',
      '                "transient": False,',
      '            },',
      '        }',
      '    return {',
      '        "schemaVersion": "frisket.runtime_projection_build_plan.v1",',
      '        "status": "accepted",',
      '        "build": {',
      '            "operation": "refresh",',
      '            "idempotencyKey": f"contract-map-points@{generation or \'unknown\'}",',
      '        },',
      '    }',
      '',
    ].join('\n'),
    'utf-8',
  );
  const requires: Array<Record<string, unknown>> = [
    { kind: 'hostCapability', id: 'projection.status' },
    { kind: 'hostCapability', id: 'host.navigation.openRow' },
  ];
  if (withDataReadCapability) {
    requires.push({ kind: 'hostCapability', id: 'projection.data.read' });
  }
  await writeFile(
    join(root, 'workbench-descriptors.json'),
    JSON.stringify(
      {
        schemaVersion: 'frisket.workbench_descriptor_package.v1',
        descriptors: [
          {
            schemaVersion: 'frisket.workbench.view.v1',
            id: viewId,
            kind: 'view',
            title: 'Contract Projection Data Read View',
            ownerPluginId: pluginId,
            componentKey: `${pluginId}.components.${viewExport}`,
            projectionKind,
            placements: [
              {
                host: 'mainView',
                mode: 'pane',
                slot: 'work.companion',
                placementId: `${pluginId}-projection-data-view`,
                order: 93,
              },
            ],
            requires,
            dataRequirements: [
              { kind: 'activeSheet' },
              { kind: 'sheetHasColumnType', columnType: 'date' },
            ],
          },
        ],
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'frontend/plugin.js'),
    `
export const ${viewExport} = ({ React, ctx }) => {
  const [state, setState] = React.useState({
    hasFetchData: 'unknown',
    fetchStatus: 'pending',
    statusOk: 'pending',
    firstBytesHex: '',
    byteLength: -1,
    bboxByteLength: -1,
    bboxStatus: 'pending',
    isArrayBuffer: 'false',
    containsRowId: 'false',
    containsLon: 'false',
    containsLat: 'false',
  });
  React.useEffect(() => {
    let active = true;
    if (!ctx || ctx.schemaVersion !== 'frisket.plugin_projection_view_context.v1') {
      setState((s) => ({ ...s, fetchStatus: 'missing-context' }));
      return () => { active = false; };
    }
    // The rest of ctx.projection is unaffected by whether
    // projection.data.read is declared (current contract, unchanged).
    ctx.projection.status().then(() => {
      if (active) setState((s) => ({ ...s, statusOk: 'resolved' }));
    }).catch(() => {
      if (active) setState((s) => ({ ...s, statusOk: 'rejected' }));
    });
    const hasFetchData = typeof ctx.projection.fetchData === 'function';
    setState((s) => ({ ...s, hasFetchData: String(hasFetchData) }));
    if (!hasFetchData) {
      setState((s) => ({ ...s, fetchStatus: 'unavailable' }));
      return () => { active = false; };
    }
    const geoColumn = (ctx.sheet?.columns ?? []).find((column) => column.type === 'geo_point');
    if (!geoColumn) {
      setState((s) => ({ ...s, fetchStatus: 'no-geo-column' }));
      return () => { active = false; };
    }
    ctx.projection.fetchData({ columnId: geoColumn.id }).then((buf) => {
      if (!active) return;
      const bytes = new Uint8Array(buf);
      const firstBytesHex = Array.from(bytes.slice(0, 4))
        .map((b) => b.toString(16).padStart(2, '0'))
        .join(',');
      const text = new TextDecoder('utf-8', { fatal: false }).decode(bytes);
      setState((s) => ({
        ...s,
        fetchStatus: 'resolved',
        firstBytesHex,
        byteLength: buf.byteLength,
        isArrayBuffer: String(buf instanceof ArrayBuffer),
        containsRowId: String(text.includes('row_id')),
        containsLon: String(text.includes('lon')),
        containsLat: String(text.includes('lat')),
      }));
      // bbox pass-through: a window around NYC keeps 1 of the 2 fixture
      // points, so the bbox'd Arrow stream must be strictly smaller than
      // the full one — proving fetchData forwards bbox to the backend's
      // R*Tree query (server/services/map_points.py) rather than dropping it.
      return ctx.projection.fetchData({
        columnId: geoColumn.id,
        bbox: [-75, 39, -73, 42],
      }).then((bboxBuf) => {
        if (!active) return;
        setState((s) => ({
          ...s,
          bboxStatus: 'resolved',
          bboxByteLength: bboxBuf.byteLength,
        }));
      });
    }).catch((err) => {
      if (!active) return;
      setState((s) => ({ ...s, fetchStatus: 'rejected:' + String(err?.message ?? err) }));
    });
    return () => { active = false; };
  }, [ctx]);
  return React.createElement('section', {
    'data-testid': 'contract-plugin-projection-data-view',
    'data-token': ${JSON.stringify(token)},
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-has-fetch-data': state.hasFetchData,
    'data-fetch-status': state.fetchStatus,
    'data-status-ok': state.statusOk,
    'data-first-bytes-hex': state.firstBytesHex,
    'data-byte-length': String(state.byteLength),
    'data-bbox-status': state.bboxStatus,
    'data-bbox-byte-length': String(state.bboxByteLength),
    'data-is-array-buffer': state.isArrayBuffer,
    'data-contains-row-id': state.containsRowId,
    'data-contains-lon': state.containsLon,
    'data-contains-lat': state.containsLat,
  }, 'contract projection data view');
};
`,
    'utf-8',
  );
}

async function setUpProjectionDataReadFixtureProject(
  request: APIRequestContext,
  namePrefix: string,
): Promise<{ projectId: string; sheetId: number; geoColumnId: string }> {
  const projectId = await createProject(request, uniqueName(namePrefix));
  const sheetId = await importCsv(
    request,
    projectId,
    'events.csv',
    'name,happened,point\nFiling,2020-01-02,\nHearing,2020-03-04,\n',
  );
  const columns = await sheetColumns(request, projectId, sheetId);
  const dateColumn = columns.find((column) => column.name === 'happened');
  expect(dateColumn).toBeTruthy();
  await setColumnType(request, projectId, dateColumn!.id, 'date');
  const pointColumn = columns.find((column) => column.name === 'point');
  expect(pointColumn).toBeTruthy();
  await setColumnType(request, projectId, pointColumn!.id, 'geo_point');
  const data = await sheetData(request, projectId, sheetId, 0, 2);
  const rows = data.rows as { id: number }[];
  await editCells(request, projectId, [
    { rowId: rows[0].id, columnId: pointColumn!.id, value: { lat: 40.7128, lon: -74.006 } },
    { rowId: rows[1].id, columnId: pointColumn!.id, value: { lat: 34.0522, lon: -118.2437 } },
  ]);
  return { projectId, sheetId, geoColumnId: String(pointColumn!.id) };
}

test('generated plugin projection view fetches projection data through ctx', async ({
  page,
}, testInfo) => {
  // Part 1: a projection view declaring hostCapability projection.data.read
  // gets ctx.projection.fetchData and receives real Arrow IPC bytes for the
  // geo_point column — the SAME map-points service/route the first-party
  // MapView reads through (server/services/map_points.py), never a raw
  // route or a JSON detour.
  const withRunSuffix = suffix();
  const withPluginId = pluginId();
  const withViewId = `${withPluginId}.view.${suffix()}`;
  const withViewExport = `ProjectionDataView${withRunSuffix}`;
  const withProjectionKind = `${withPluginId}.projection.${suffix()}`;
  const withToken = `contract-token-${withRunSuffix}`;
  const withRoot = resolve(testInfo.outputPath('generated-projection-data-read-plugin'));
  await writeProjectionDataReadPluginPackage({
    root: withRoot,
    pluginId: withPluginId,
    viewId: withViewId,
    viewExport: withViewExport,
    projectionKind: withProjectionKind,
    token: withToken,
    withDataReadCapability: true,
  });
  const withFixture = await setUpProjectionDataReadFixtureProject(
    page.request,
    'plugin-contract-projection-data-read',
  );
  await installAndActivatePlugin(page.request, withFixture.projectId, withPluginId, withRoot, {
    // The fixture owns a role-map_points projection binding (plugin-owned
    // map-points world) — backend activation registers it.
    permissionsAccepted: ['plugin:trusted_local_backend'],
    backendActivate: true,
  });
  await openProject(page, withFixture.projectId, withFixture.sheetId);

  const withView = page.getByTestId('contract-plugin-projection-data-view');
  await expect(withView).toBeVisible();
  await expect(withView).toHaveAttribute('data-token', withToken);
  await expect(withView).toHaveAttribute(
    'data-schema',
    'frisket.plugin_projection_view_context.v1',
  );
  await expect(withView).toHaveAttribute('data-has-fetch-data', 'true');
  await expect(withView).toHaveAttribute('data-fetch-status', 'resolved');
  // status() targets the fixture's made-up projectionKind, which has no
  // registered backend projection (same as the plain projection-view test
  // above) — settling either way proves the host wiring executes; the
  // rest of ctx.projection is unaffected by projection.data.read either
  // way, which is the point of this assertion.
  await expect(withView).toHaveAttribute('data-status-ok', /^(resolved|rejected)$/);
  await expect(withView).toHaveAttribute('data-is-array-buffer', 'true');
  // The real Arrow IPC stream continuation marker
  // (frisket.projections.point_wire.serialize_map_points_arrow pipes
  // through pa.ipc.new_stream — NOT the "ARROW1" File-format magic, which
  // this transport never emits) — the first 4 bytes of every message.
  await expect(withView).toHaveAttribute('data-first-bytes-hex', 'ff,ff,ff,ff');
  await expect(withView).toHaveAttribute('data-contains-row-id', 'true');
  await expect(withView).toHaveAttribute('data-contains-lon', 'true');
  await expect(withView).toHaveAttribute('data-contains-lat', 'true');
  const byteLength = Number(await withView.getAttribute('data-byte-length'));
  expect(byteLength).toBeGreaterThan(8);
  // bbox pass-through: the
  // NYC-window bbox keeps 1 of the 2 fixture points, so the bbox'd stream is
  // strictly smaller — fetchData forwards bbox to the backend's R*Tree query.
  await expect(withView).toHaveAttribute('data-bbox-status', 'resolved');
  const bboxByteLength = Number(await withView.getAttribute('data-bbox-byte-length'));
  expect(bboxByteLength).toBeGreaterThan(8);
  expect(bboxByteLength).toBeLessThan(byteLength);

  // Part 2: a sibling projection view that does NOT declare
  // projection.data.read gets no ctx.projection.fetchData — the rest of
  // ctx.projection (status/build/readArtifact) is unaffected.
  const withoutRunSuffix = suffix();
  const withoutPluginId = pluginId();
  const withoutViewId = `${withoutPluginId}.view.${suffix()}`;
  const withoutViewExport = `ProjectionDataView${withoutRunSuffix}`;
  const withoutProjectionKind = `${withoutPluginId}.projection.${suffix()}`;
  const withoutToken = `contract-token-${withoutRunSuffix}`;
  const withoutRoot = resolve(
    testInfo.outputPath('generated-projection-data-read-plugin-sibling'),
  );
  await writeProjectionDataReadPluginPackage({
    root: withoutRoot,
    pluginId: withoutPluginId,
    viewId: withoutViewId,
    viewExport: withoutViewExport,
    projectionKind: withoutProjectionKind,
    token: withoutToken,
    withDataReadCapability: false,
  });
  const withoutFixture = await setUpProjectionDataReadFixtureProject(
    page.request,
    'plugin-contract-projection-data-read-sibling',
  );
  await installAndActivatePlugin(
    page.request,
    withoutFixture.projectId,
    withoutPluginId,
    withoutRoot,
    { permissionsAccepted: ['plugin:trusted_local_backend'], backendActivate: true },
  );
  await openProject(page, withoutFixture.projectId, withoutFixture.sheetId);

  const withoutView = page.getByTestId('contract-plugin-projection-data-view');
  await expect(withoutView).toBeVisible();
  await expect(withoutView).toHaveAttribute('data-token', withoutToken);
  await expect(withoutView).toHaveAttribute('data-has-fetch-data', 'false');
  await expect(withoutView).toHaveAttribute('data-fetch-status', 'unavailable');
  await expect(withoutView).toHaveAttribute('data-status-ok', /^(resolved|rejected)$/);
});

function layoutItemTestId(contributionId: string): string {
  return `workbench-resolved-layout-item-${contributionId.replace(/[^a-zA-Z0-9]+/g, '-')}`;
}

async function writeCommandPluginPackage({
  root,
  pluginId,
  commandId,
  badCommandContributionId,
  handlerExport,
  token,
  includeBadCommand = false,
}: {
  root: string;
  pluginId: string;
  commandId: string;
  badCommandContributionId: string;
  handlerExport: string;
  token: string;
  includeBadCommand?: boolean;
}) {
  await mkdir(join(root, 'frontend'), { recursive: true });
  await writeFile(
    join(root, 'plugin.json'),
    JSON.stringify(
      {
        schema_version: 'frisket.plugin.v1',
        id: pluginId,
        version: '0.1.0',
        contributes: {
          workbench_views: [],
          workbench_panels: [],
          workbench_commands: includeBadCommand
            ? [commandId, badCommandContributionId]
            : [commandId],
          actions: [],
          importers: [],
          operators: [],
          projections: [],
          column_types: [],
          job_handlers: [],
        },
        requires: { capabilities: [], secrets: [] },
        runtime: {
          actions: [],
          importers: [],
          operators: [],
          projections: [],
          job_handlers: [],
          workbench_components: [
            {
              contribution_id: commandId,
              module_key: `${pluginId}.ui`,
              component_key: `${pluginId}.commands.${handlerExport}`,
              module_path: 'frontend/plugin.js',
            },
            ...(includeBadCommand
              ? [
                  {
                    contribution_id: badCommandContributionId,
                    module_key: `${pluginId}.ui`,
                    component_key: `${pluginId}.commands.${handlerExport}`,
                    module_path: 'frontend/plugin.js',
                  },
                ]
              : []),
          ],
        },
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'workbench-descriptors.json'),
    JSON.stringify(
      {
        schemaVersion: 'frisket.workbench_descriptor_package.v1',
        descriptors: [
          {
            schemaVersion: 'frisket.command.v1',
            id: commandId,
            kind: 'command',
            title: 'Contract Command',
            ownerPluginId: pluginId,
            componentKey: `${pluginId}.commands.${handlerExport}`,
            commandId,
            handlerKey: `${pluginId}.commands.${handlerExport}`,
            placements: [
              {
                host: 'commandPalette',
                mode: 'command',
                slot: 'launcher',
                placementId: `${pluginId}-command`,
                order: 90,
              },
            ],
            requires: [],
            dataRequirements: [],
          },
          ...(includeBadCommand
            ? [
              {
                schemaVersion: 'frisket.command.v1',
                id: badCommandContributionId,
                kind: 'command',
                title: 'Contract Shadowing Command',
                ownerPluginId: pluginId,
                componentKey: `${pluginId}.commands.${handlerExport}`,
                // NOT namespaced under the plugin id: the frontend parser must
                // reject this whole descriptor (it could shadow core commands).
                commandId: 'frisket.core.command.open_sources',
                handlerKey: `${pluginId}.commands.${handlerExport}`,
                placements: [
                  {
                    host: 'commandPalette',
                    mode: 'command',
                    slot: 'launcher',
                    placementId: `${pluginId}-bad-command`,
                    order: 91,
                  },
                ],
                requires: [],
                dataRequirements: [],
              },
            ]
            : []),
        ],
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'frontend/plugin.js'),
    `
export const ${handlerExport} = ({ ctx }) => {
  // Counter (not an idempotent stamp) so a double invocation is detectable.
  const previous = Number(document.body.dataset.pluginCommandRunCount ?? '0');
  document.body.dataset.pluginCommandRunCount = String(previous + 1);
  document.body.dataset.pluginCommandRan = ctx?.commandId ?? 'missing';
  document.body.dataset.pluginCommandSchema = ctx?.schemaVersion ?? 'missing';
  document.body.dataset.pluginCommandSheet = ctx?.sheet?.name ?? 'none';
  document.body.dataset.pluginCommandToken = ${JSON.stringify(token)};
  document.body.dataset.pluginCommandSelectedCount = String(ctx?.selection?.selectedCount ?? -1);
  document.body.dataset.pluginCommandCanOpen = String(typeof ctx?.navigation?.openRow === 'function');
  // Exercise the capability-bearing context member: open the first selected
  // row through the host (the invocation-scoped snapshot carries selection).
  const rowId = ctx?.selection?.selectedRowIds?.[0];
  if (rowId && ctx?.navigation) {
    ctx.navigation.openRow(rowId);
  }
};
`,
    'utf-8',
  );
}

test('plugin descriptor with an unsupported placement fails closed and does not mount', async ({
  page,
}, testInfo) => {
  const runSuffix = suffix();
  const generatedPluginId = pluginId();
  const viewId = `${generatedPluginId}.view.${suffix()}`;
  const panelId = `${generatedPluginId}.panel.${suffix()}`;
  const viewExport = `View${runSuffix}`;
  const panelExport = `Panel${runSuffix}`;
  const token = `contract-token-${runSuffix}`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-illegal-placement'));
  // commandPalette:panel is backend-legal vocabulary but is not (and will never
  // be) a plugin-legal pair for panel descriptors — the whole descriptor must be
  // rejected, including its legal rightInspector placement (no partial mounts).
  await writeRuntimeUiPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    viewId,
    panelId,
    viewExport,
    panelExport,
    token,
    panelPlacements: [
      {
        host: 'rightInspector',
        mode: 'panel',
        slot: 'inspection',
        placementId: `${generatedPluginId}-right-inspector`,
        order: 91,
      },
      {
        host: 'commandPalette',
        mode: 'panel',
        placementId: `${generatedPluginId}-illegal`,
        order: 92,
      },
    ],
  });

  const projectId = await createProject(
    page.request,
    uniqueName('plugin-contract-illegal-placement'),
  );
  const sheetId = await importCsv(
    page.request,
    projectId,
    'people.csv',
    'name,city\nAda,London\nGrace,Arlington\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  // The sibling view descriptor is fully legal and proves the plugin is active.
  await expect(page.getByTestId(testIdForContribution(viewId))).toBeVisible();
  // The panel descriptor is rejected whole: no frame, no component, no resolved
  // layout metadata for either of its declared placements.
  await expect(page.getByTestId(testIdForContribution(panelId))).toHaveCount(0);
  await expect(page.getByTestId('contract-plugin-panel')).toHaveCount(0);
  await expect(page.getByTestId(layoutItemTestId(panelId))).toHaveCount(0);
});

test('plugin panel descriptor with multiple legal placements resolves every placement', async ({
  page,
}, testInfo) => {
  const runSuffix = suffix();
  const generatedPluginId = pluginId();
  const viewId = `${generatedPluginId}.view.${suffix()}`;
  const panelId = `${generatedPluginId}.panel.${suffix()}`;
  const viewExport = `View${runSuffix}`;
  const panelExport = `Panel${runSuffix}`;
  const token = `contract-token-${runSuffix}`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-multi-placement'));
  await writeRuntimeUiPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    viewId,
    panelId,
    viewExport,
    panelExport,
    token,
    panelPlacements: [
      {
        host: 'rightInspector',
        mode: 'panel',
        slot: 'inspection',
        placementId: `${generatedPluginId}-inspector-a`,
        order: 91,
      },
      {
        host: 'rightInspector',
        mode: 'panel',
        slot: 'inspection',
        placementId: `${generatedPluginId}-inspector-b`,
        order: 92,
      },
    ],
  });

  const projectId = await createProject(
    page.request,
    uniqueName('plugin-contract-multi-placement'),
  );
  const sheetId = await importCsv(
    page.request,
    projectId,
    'people.csv',
    'name,city\nAda,London\nGrace,Arlington\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  // The panel mounts and both declared placements resolve in layout metadata.
  await expect(page.getByTestId(testIdForContribution(panelId))).toBeVisible();
  await expect(page.getByTestId('contract-plugin-panel')).toBeVisible();
  await expect(page.getByTestId(layoutItemTestId(panelId))).toHaveCount(2);
  const placementIds = await page
    .getByTestId(layoutItemTestId(panelId))
    .evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-placement-id')));
  expect(placementIds.sort()).toEqual([
    `${generatedPluginId}-inspector-a`,
    `${generatedPluginId}-inspector-b`,
  ]);
});

test('generated plugin dock tab mounts with dock tab context and hiding it falls back to jobs', async ({
  page,
}, testInfo) => {
  const runSuffix = suffix();
  const generatedPluginId = pluginId();
  const viewId = `${generatedPluginId}.view.${suffix()}`;
  const panelId = `${generatedPluginId}.panel.${suffix()}`;
  const viewExport = `View${runSuffix}`;
  const panelExport = `Panel${runSuffix}`;
  const token = `contract-token-${runSuffix}`;
  const dockPlacementId = `${generatedPluginId}-dock-tab`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-dock-tab'));
  await writeRuntimeUiPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    viewId,
    panelId,
    viewExport,
    panelExport,
    token,
    panelPlacements: [
      {
        host: 'bottomDock',
        mode: 'tab',
        slot: 'companion.output',
        placementId: dockPlacementId,
        order: 90,
        tabChrome: { closeable: true, singleton: true },
      },
    ],
  });

  const projectId = await createProject(
    page.request,
    uniqueName('plugin-contract-dock-tab'),
  );
  const sheetId = await importCsv(
    page.request,
    projectId,
    'people.csv',
    'name,city\nAda,London\nGrace,Arlington\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  // The dock tab renders from the resolved runtime descriptor, not a fixed list.
  const dockTab = page.getByTestId(`bottom-dock-tab-${dockPlacementId}`);
  await expect(dockTab).toBeVisible();
  await expect(dockTab).toHaveAttribute('data-contribution-id', panelId);
  await expect(dockTab).toHaveAttribute('data-runtime-source', 'runtimeIndex');

  await dockTab.click();
  const frame = page.getByTestId(testIdForContribution(panelId));
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute('data-plugin-dock-tab-status', 'mounted');
  await expect(frame).toHaveAttribute(
    'data-plugin-dock-tab-context-schema-version',
    'frisket.plugin_dock_tab_context.v1',
  );
  const panel = page.getByTestId('contract-plugin-panel');
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('data-schema', 'frisket.plugin_dock_tab_context.v1');
  await expect(panel).toHaveAttribute('data-token', token);
  await expect(panel).toHaveAttribute('data-sheet-name', 'people');
  await expect(panel).toHaveAttribute('data-dock-active', 'true');
  await expect(panel).toHaveAttribute('data-can-dock-focus', 'true');

  // tabChrome.closeable renders a close affordance that hides through the
  // visibility mechanism — the ACTIVE plugin tab disappears and the dock falls
  // back to the non-hideable jobs anchor — the same tab-list recomputation a
  // mid-session uninstall/disable takes.
  await page.getByTestId(`bottom-dock-tab-close-${dockPlacementId}`).click();
  await expect(page.getByTestId(`bottom-dock-tab-${dockPlacementId}`)).toHaveCount(0);
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-placement-id',
    'jobs',
  );
  await expect(page.getByTestId('contract-plugin-panel')).toHaveCount(0);

  // Disable via the lifecycle API + index refresh removes the tab entirely
  // (reveal first so removal is attributable to disable, not the hide). Reveal
  // runs through the ⌘K palette's visibility command (the Window menu retired,
  // workbench-ia-toolbar-diet-v1).
  const revealPanelSlug = panelId.replace(/[^a-zA-Z0-9]+/g, '-');
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const revealPalette = page.getByTestId('workbench-region-commandPalette');
  await expect(revealPalette).toBeVisible();
  await revealPalette.getByTestId(`workbench-visibility-command-reveal-${revealPanelSlug}`).click();
  await page.getByLabel('Close command palette').click();
  await expect(page.getByTestId(`bottom-dock-tab-${dockPlacementId}`)).toBeVisible();
  // Lifecycle POSTs rescan the package on a busy dev server; give this one
  // more headroom than the 8s default action timeout (assertion unchanged).
  const disabled = await page.request.post(
    `/api/projects/${projectId}/workbench/plugins/${generatedPluginId}/disable`,
    { timeout: 30_000 },
  );
  expect(disabled.ok()).toBeTruthy();
  // The Plugins dock tab (the prior on-demand runtime-index refresh trigger)
  // retired, but
  // its refresh-on-select moved to the Errors tab instead of disappearing
  // (Errors is
  // where plugin failures surface now, so it is the tab that actually needs
  // this freshness). Selecting it — NOT a page reload — must be enough to
  // observe a mid-session lifecycle change: contributed dock panels and the
  // Errors tab's synthetic plugin-failure rows share the same runtime-index
  // store field, so proving one reconciles without a reload proves both do.
  await page.getByTestId('bottom-dock-tab-errors').click();
  await expect(page.getByTestId(`bottom-dock-tab-${dockPlacementId}`)).toHaveCount(0);
});

// refreshWorkbenchPluginRuntimeIndex runs on a 30s background poll
// PLUS Errors-tab selection, but it still cleared the runtime index to null
// on ANY fetch failure — including one transient blip on an otherwise
// healthy session. pluginPanelDescriptorsFromRuntimeIndex(null) returns no
// descriptors, so that null wipe silently unmounted every plugin-CONTRIBUTED
// dock panel (WorkbenchBottomDock falls back to Jobs when the active
// placement vanishes) even though nothing about the plugin actually
// changed. This test proves that a transient refresh failure
// with a GOOD prior snapshot must leave a mounted contributed panel alone,
// and a later successful refresh must still reconcile normally afterward.
test('a transient runtime-index refresh failure does not remove a mounted contributed dock panel', async ({
  page,
}, testInfo) => {
  const runSuffix = suffix();
  const generatedPluginId = pluginId();
  const viewId = `${generatedPluginId}.view.${suffix()}`;
  const panelId = `${generatedPluginId}.panel.${suffix()}`;
  const viewExport = `View${runSuffix}`;
  const panelExport = `Panel${runSuffix}`;
  const token = `contract-token-${runSuffix}`;
  const dockPlacementId = `${generatedPluginId}-dock-tab`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-dock-tab-transient-failure'));
  await writeRuntimeUiPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    viewId,
    panelId,
    viewExport,
    panelExport,
    token,
    panelPlacements: [
      {
        host: 'bottomDock',
        mode: 'tab',
        slot: 'companion.output',
        placementId: dockPlacementId,
        order: 90,
        tabChrome: { closeable: true, singleton: true },
      },
    ],
  });

  const projectId = await createProject(
    page.request,
    uniqueName('plugin-contract-transient-refresh-failure'),
  );
  const sheetId = await importCsv(
    page.request,
    projectId,
    'people.csv',
    'name,city\nAda,London\nGrace,Arlington\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  const dockTab = page.getByTestId(`bottom-dock-tab-${dockPlacementId}`);
  await expect(dockTab).toBeVisible();
  await dockTab.click();
  const panel = page.getByTestId('contract-plugin-panel');
  await expect(panel).toBeVisible();

  // Fail exactly ONE runtime-index GET (real requests otherwise pass
  // through untouched — a genuine transient blip, not a permanently broken
  // endpoint), then let every subsequent request through to the real
  // backend again.
  let failedOnce = false;
  await page.route(`**/api/projects/${projectId}/workbench/plugins`, async (route) => {
    if (route.request().method() === 'GET' && !failedOnce) {
      failedOnce = true;
      await route.fulfill({
        status: 500,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'injected transient failure' }),
      });
      return;
    }
    await route.continue();
  });

  // Errors-tab selection is the explicit refresh trigger — it fires straight
  // into the injected failure above. Switching the active dock tab away from
  // the plugin's own tab unmounts its panel CONTENT regardless of the fetch
  // outcome (the dock only ever renders the active tab's content) — that is
  // normal tab-switch behavior, not the bug under test. The bug under test
  // is whether the TAB ITSELF survives in the tablist: if the failed refresh
  // had wiped the runtime index to null, pluginPanelDescriptorsFromRuntimeIndex(null)
  // would return no descriptors and the tab would disappear entirely (the
  // same observable effect the earlier disable-reconciliation test in this
  // file asserts on success).
  await page.getByTestId('bottom-dock-tab-errors').click();
  expect(failedOnce).toBe(true);
  await expect(dockTab).toBeVisible();
  await expect(dockTab).toHaveAttribute('data-contribution-id', panelId);

  // Switching back mounts the SAME contributed panel — still the resolved
  // descriptor, still carrying its original token (proving it is the real
  // plugin panel reconciled from a preserved runtime index, not a
  // coincidentally-similar stand-in).
  await dockTab.click();
  await expect(page.getByTestId(testIdForContribution(panelId))).toHaveAttribute(
    'data-plugin-dock-tab-status',
    'mounted',
  );
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('data-token', token);

  // The route now lets every request through for real — the next refresh
  // must reconcile normally, proving the failure didn't wedge anything.
  await page.getByTestId('bottom-dock-tab-errors').click();
  await expect(dockTab).toBeVisible();
  await dockTab.click();
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('data-token', token);
});

test('generated plugin command runs from the palette with an invocation-scoped context', async ({
  page,
}, testInfo) => {
  const runSuffix = suffix();
  const generatedPluginId = pluginId();
  const commandId = `${generatedPluginId}.command.${suffix()}`;
  const badCommandContributionId = `${generatedPluginId}.command.bad${suffix()}`;
  const handlerExport = `Handler${runSuffix}`;
  const token = `contract-token-${runSuffix}`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-command'));
  await writeCommandPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    commandId,
    badCommandContributionId,
    handlerExport,
    token,
  });

  const projectId = await createProject(
    page.request,
    uniqueName('plugin-contract-command'),
  );
  const sheetId = await importCsv(
    page.request,
    projectId,
    'people.csv',
    'name,city\nAda,London\nGrace,Arlington\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);
  // Select a row so the invocation snapshot carries an active row the handler
  // can open through ctx.navigation.
  await selectRow(page, 0);

  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const palette = page.getByTestId('workbench-region-commandPalette');
  await expect(palette).toBeVisible();

  const commandSlug = commandId.replace(/[^a-zA-Z0-9]+/g, '-');
  const commandButton = palette.getByTestId(`workbench-command-${commandSlug}`);
  await expect(commandButton).toBeVisible();
  await expect(commandButton).toHaveAttribute('data-command-id', commandId);
  await expect(commandButton).toHaveAttribute(
    'data-runtime-handler-key',
    `${generatedPluginId}.commands.${handlerExport}`,
  );
  await expect(commandButton).toHaveAttribute('data-host', 'commandPalette');
  await expect(commandButton).toHaveAttribute('data-mode', 'command');
  // A command whose commandId is not namespaced under the owning plugin id
  // now fails INSTALL outright — proven below after the good flow;
  // the frontend parser's prefix rejection remains as defense in depth.

  // No auto-invocation: opening the palette must not run any handler.
  await expect(page.locator('body')).not.toHaveAttribute('data-plugin-command-ran');
  await expect(page.locator('body')).not.toHaveAttribute('data-plugin-command-run-count');

  await commandButton.click();
  // Exactly one invocation per click (counter, not an idempotent stamp).
  await expect(page.locator('body')).toHaveAttribute('data-plugin-command-run-count', '1');
  await expect(page.locator('body')).toHaveAttribute('data-plugin-command-ran', commandId);
  await expect(page.locator('body')).toHaveAttribute(
    'data-plugin-command-schema',
    'frisket.plugin_command_context.v1',
  );
  await expect(page.locator('body')).toHaveAttribute('data-plugin-command-sheet', 'people');
  await expect(page.locator('body')).toHaveAttribute('data-plugin-command-token', token);
  await expect(page.locator('body')).toHaveAttribute('data-plugin-command-can-open', 'true');
  await expect(page.locator('body')).toHaveAttribute(
    'data-plugin-command-selected-count',
    '1',
  );
  // The handler opened the selected row through ctx.navigation — the row
  // drawer is the host-side proof the capability-bearing member is wired.
  await expect(page.getByTestId('row-drawer')).toBeVisible();
  await expect(page.getByTestId('row-drawer')).toContainText('Ada');

  // The smuggled-commandId package is now rejected at INSTALL (stronger than
  // the old render-time drop): the backend namespacing rule refuses it.
  const badPluginId = pluginId();
  const badRoot = resolve(testInfo.outputPath('generated-plugin-bad-command'));
  await writeCommandPluginPackage({
    root: badRoot,
    pluginId: badPluginId,
    commandId: `${badPluginId}.command.good`,
    badCommandContributionId: `${badPluginId}.command.bad`,
    handlerExport,
    token,
    includeBadCommand: true,
  });
  const badInstall = await page.request.post(
    `/api/projects/${projectId}/workbench/plugins/${badPluginId}/install-local`,
    {
      data: {
        source: { kind: 'localPath', value: badRoot },
        arbitraryPackageLoadAllowed: false,
      },
    },
  );
  expect(badInstall.ok()).toBeFalsy();
  expect(await badInstall.text()).toContain('commandId equal to the contribution id');
});

// NEW HOST: a plugin panel that
// DECLARES the leftSidebar host re-homes as a Discover tab appended after the
// four first-party tabs (the descriptor host stays leftSidebar; the workbench
// re-maps it at render time). The ⌘K palette's visibility command hides/reveals
// the tab. Full plugin-author flow: install → contribute → render → hide/reveal.
test('generated plugin panel hosts as a Discover tab with the panel context host', async ({
  page,
}, testInfo) => {
  const runSuffix = suffix();
  const generatedPluginId = pluginId();
  const viewId = `${generatedPluginId}.view.${suffix()}`;
  const panelId = `${generatedPluginId}.panel.${suffix()}`;
  const viewExport = `View${runSuffix}`;
  const panelExport = `Panel${runSuffix}`;
  const token = `contract-token-${runSuffix}`;
  const sidebarPlacementId = `${generatedPluginId}-sidebar`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-sidebar'));
  await writeRuntimeUiPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    viewId,
    panelId,
    viewExport,
    panelExport,
    token,
    panelPlacements: [
      {
        host: 'leftSidebar',
        mode: 'panel',
        slot: 'scope',
        placementId: sidebarPlacementId,
        order: 120,
      },
    ],
  });

  const projectId = await createProject(
    page.request,
    uniqueName('plugin-contract-sidebar'),
  );
  const sheetId = await importCsv(
    page.request,
    projectId,
    'people.csv',
    'name,city\nAda,London\nGrace,Arlington\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);
  void sidebarPlacementId;

  // The Discover region appends a tab for the plugin panel. The panel is only
  // 290px wide, so the tab can sit in the `»` overflow (or, when the region is
  // collapsed, on the rail) — activate it the way a user would.
  const panelSlug = panelId.replace(/[^a-zA-Z0-9]+/g, '-');
  const openPluginDiscoverTab = async () => {
    const discoverPanel = page.getByTestId('discover-panel');
    const rail = page.getByTestId('discover-rail');
    await expect(discoverPanel.or(rail).first()).toBeVisible();
    if (await rail.isVisible()) {
      await page.getByTestId(`discover-rail-icon-${panelSlug}`).click();
    } else {
      const tabButton = page.getByTestId(`discover-tab-${panelSlug}`);
      if (await tabButton.isVisible()) {
        await tabButton.click();
      } else {
        await page.getByTestId('discover-tab-overflow').click();
        await page.getByTestId(`discover-tab-menu-${panelSlug}`).click();
      }
    }
    await expect(page.getByTestId(`discover-tab-${panelSlug}`)).toHaveAttribute(
      'aria-selected',
      'true',
    );
  };
  await openPluginDiscoverTab();

  // The plugin frame + component mount in the Discover body; the panel context
  // still reports its DECLARED placement host (leftSidebar), contract intact.
  const frame = page.getByTestId(testIdForContribution(panelId));
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute('data-plugin-panel-status', 'mounted');
  const panel = page.getByTestId('contract-plugin-panel');
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('data-schema', 'frisket.plugin_panel_context.v1');
  await expect(panel).toHaveAttribute('data-placement-host', 'leftSidebar');
  await expect(panel).toHaveAttribute('data-token', token);
  await expect(panel).toHaveAttribute('data-sheet-name', 'people');

  // The ⌘K palette's visibility command manages it (the Window menu retired,
  // workbench-ia-toolbar-diet-v1): hide drops the Discover tab entirely.
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const hidePalette = page.getByTestId('workbench-region-commandPalette');
  await expect(hidePalette).toBeVisible();
  await hidePalette.getByTestId(`workbench-visibility-command-hide-${panelSlug}`).click();
  await page.getByLabel('Close command palette').click();
  await expect(page.getByTestId(`discover-tab-${panelSlug}`)).toHaveCount(0);
  await expect(page.getByTestId('contract-plugin-panel')).toHaveCount(0);
  // Recovery is palette-only (the activity rail retired with the redesign):
  // the reveal command carries the hidden state and restores the tab.
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  const revealPalette = page.getByTestId('workbench-region-commandPalette');
  await expect(revealPalette).toBeVisible();
  const reveal = revealPalette.getByTestId(`workbench-visibility-command-reveal-${panelSlug}`);
  await expect(reveal).toHaveAttribute('data-availability-status', 'hidden');
  await reveal.click();
  await page.getByLabel('Close command palette').click();
  await openPluginDiscoverTab();
  await expect(page.getByTestId('contract-plugin-panel')).toBeVisible();
});

async function writeDetailPluginPackage({
  root,
  pluginId,
  rowId,
  columnId,
  entityId,
  sourceId,
  handlerSuffix,
}: {
  root: string;
  pluginId: string;
  rowId: string;
  columnId: string;
  entityId: string;
  sourceId: string;
  handlerSuffix: string;
}) {
  const bindings = [
    { id: rowId, exportName: `RowTab${handlerSuffix}`, host: 'rowDetail', mode: 'tab', testId: 'contract-detail-row-tab' },
    { id: columnId, exportName: `ColumnSection${handlerSuffix}`, host: 'columnInspector', mode: 'section', testId: 'contract-detail-column-section' },
    { id: `${columnId}_tab`, exportName: `ColumnTab${handlerSuffix}`, host: 'columnDetail', mode: 'tab', testId: 'contract-detail-column-tab' },
    { id: entityId, exportName: `EntityTab${handlerSuffix}`, host: 'entityDetail', mode: 'tab', testId: 'contract-detail-entity-tab' },
    { id: sourceId, exportName: `SourceTab${handlerSuffix}`, host: 'sourceDetail', mode: 'tab', testId: 'contract-detail-source-tab' },
  ];
  await mkdir(join(root, 'frontend'), { recursive: true });
  await writeFile(
    join(root, 'plugin.json'),
    JSON.stringify({
      schema_version: 'frisket.plugin.v1',
      id: pluginId,
      version: '0.1.0',
      contributes: {
        workbench_views: [],
        workbench_panels: bindings.map((binding) => binding.id),
        actions: [], importers: [], operators: [], projections: [], column_types: [], job_handlers: [],
      },
      requires: { capabilities: [], secrets: [] },
      runtime: {
        actions: [], importers: [], operators: [], projections: [], job_handlers: [],
        workbench_components: bindings.map((binding) => ({
          contribution_id: binding.id,
          module_key: `${pluginId}.ui`,
          component_key: `${pluginId}.components.${binding.exportName}`,
          module_path: 'frontend/plugin.js',
        })),
      },
    }, null, 2),
    'utf-8',
  );
  await writeFile(
    join(root, 'workbench-descriptors.json'),
    JSON.stringify({
      schemaVersion: 'frisket.workbench_descriptor_package.v1',
      descriptors: bindings.map((binding, index) => ({
        schemaVersion: 'frisket.workbench.panel.v1',
        id: binding.id,
        kind: 'panel',
        title: `Contract ${binding.exportName}`,
        shortTitle: binding.exportName,
        ownerPluginId: pluginId,
        componentKey: `${pluginId}.components.${binding.exportName}`,
        placements: [{
          host: binding.host,
          mode: binding.mode,
          slot: 'detail',
          placementId: `${pluginId}-${binding.host}`,
          order: 100 + index,
        }],
        requires: [{ kind: 'hostCapability', id: 'sheet.active' }],
        dataRequirements: [{ kind: 'activeSheet' }],
      })),
    }, null, 2),
    'utf-8',
  );
  await writeFile(
    join(root, 'frontend/plugin.js'),
    bindings
      .map(
        (binding) => `
export const ${binding.exportName} = ({ React, ctx }) =>
  React.createElement('section', {
    'data-testid': ${JSON.stringify(binding.testId)},
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-subject-kind': ctx?.detail?.subject?.kind ?? 'missing',
    'data-subject': JSON.stringify(ctx?.detail?.subject ?? null),
  }, 'contract detail contribution');
`,
      )
      .join('\n'),
    'utf-8',
  );
}

test('generated plugin row detail tab mounts in the row drawer with a row subject', async ({
  page,
}, testInfo) => {
  const generatedPluginId = pluginId();
  const handlerSuffix = suffix();
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-detail-row'));
  await writeDetailPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    rowId: `${generatedPluginId}.panel.row`,
    columnId: `${generatedPluginId}.panel.column`,
    entityId: `${generatedPluginId}.panel.entity`,
    sourceId: `${generatedPluginId}.panel.source`,
    handlerSuffix,
  });
  const projectId = await createProject(page.request, uniqueName('plugin-contract-detail-row'));
  const sheetId = await importCsv(
    page.request, projectId, 'people.csv', 'name,city\nAda,London\nGrace,Arlington\n',
  );
  const data = await sheetData(page.request, projectId, sheetId, 0, 1);
  const firstRowId = String(data.rows[0].id);
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  const columns = await sheetColumns(page.request, projectId, sheetId);
  await clickCell(page, columns, 'name', 0);
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('row-drawer')).toBeVisible();

  const wrapper = page.getByTestId(
    `row-detail-contribution-${`${generatedPluginId}.panel.row`.replace(/[^a-zA-Z0-9]+/g, '-')}`,
  );
  await expect(wrapper).toBeVisible();
  await expect(wrapper).toHaveAttribute('data-runtime-source', 'runtimeIndex');
  const tab = page.getByTestId('contract-detail-row-tab');
  await expect(tab).toBeVisible();
  await expect(tab).toHaveAttribute('data-schema', 'frisket.plugin_detail_context.v1');
  await expect(tab).toHaveAttribute('data-subject-kind', 'row');
  await expect(tab).toHaveAttribute(
    'data-subject',
    JSON.stringify({ kind: 'row', sheetId: String(sheetId), rowId: firstRowId }),
  );
});

test('generated plugin column section mounts in the column drawer with a column subject', async ({
  page,
}, testInfo) => {
  const generatedPluginId = pluginId();
  const handlerSuffix = suffix();
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-detail-column'));
  await writeDetailPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    rowId: `${generatedPluginId}.panel.row`,
    columnId: `${generatedPluginId}.panel.column`,
    entityId: `${generatedPluginId}.panel.entity`,
    sourceId: `${generatedPluginId}.panel.source`,
    handlerSuffix,
  });
  const projectId = await createProject(page.request, uniqueName('plugin-contract-detail-column'));
  const sheetId = await importCsv(
    page.request, projectId, 'people.csv', 'name,city\nAda,London\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  const columns = await sheetColumns(page.request, projectId, sheetId);
  const nameColumn = columns.find((column) => column.name === 'name');
  expect(nameColumn).toBeTruthy();
  await clickHeaderMenu(page, columns, 'name');
  await page.getByTestId('header-menu-column-settings').click();
  await expect(page.getByTestId('column-drawer')).toBeVisible();

  const section = page.getByTestId('contract-detail-column-section');
  await expect(section).toBeVisible();
  await expect(section).toHaveAttribute('data-schema', 'frisket.plugin_detail_context.v1');
  await expect(section).toHaveAttribute('data-subject-kind', 'column');
  await expect(section).toHaveAttribute(
    'data-subject',
    JSON.stringify({
      kind: 'column',
      sheetId: String(sheetId),
      columnId: String(nameColumn!.id),
      columnName: 'name',
    }),
  );

  // The columnDetail:tab placement mounts in the same drawer with the same
  // subject (the cohort's fifth allowlist pair).
  const columnTab = page.getByTestId('contract-detail-column-tab');
  await expect(columnTab).toBeVisible();
  await expect(columnTab).toHaveAttribute('data-schema', 'frisket.plugin_detail_context.v1');
  await expect(columnTab).toHaveAttribute('data-subject-kind', 'column');
  await expect(columnTab).toHaveAttribute(
    'data-subject',
    JSON.stringify({
      kind: 'column',
      sheetId: String(sheetId),
      columnId: String(nameColumn!.id),
      columnName: 'name',
    }),
  );
});

test('generated plugin detail tabs mount in entity and source detail with their subjects', async ({
  page,
}, testInfo) => {
  const generatedPluginId = pluginId();
  const handlerSuffix = suffix();
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-detail-entity-source'));
  await writeDetailPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    rowId: `${generatedPluginId}.panel.row`,
    columnId: `${generatedPluginId}.panel.column`,
    entityId: `${generatedPluginId}.panel.entity`,
    sourceId: `${generatedPluginId}.panel.source`,
    handlerSuffix,
  });
  const projectId = await createProject(
    page.request,
    uniqueName('plugin-contract-detail-entity-source'),
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);

  // Entity detail hosts inside the graph view (graph-view-generic-ui-v1). The
  // graph segment is now data-keyed to materialized edge/join sheets and the
  // FtM anchor/schema toolbar was retired, so seed a derive.join edge sheet,
  // open it, and SELECT a node to surface the entity-detail shell + the
  // plugin's entity tab (subject.kind === 'entity').
  const leftId = await importCsv(
    page.request,
    projectId,
    'people.csv',
    'code,name\nP1,Jane Smith\nP2,Acme LLC\n',
  );
  const rightId = await importCsv(
    page.request,
    projectId,
    'roles.csv',
    'code,role\nP1,Director\nP2,Owner\n',
  );
  const joinRes = await page.request.post(`/api/projects/${projectId}/actions/v1/run`, {
    data: {
      schema_version: 'frisket.action.v2',
      kind: 'derive.join',
      capabilities: ['project:write'],
      params: {
        left_sheet_id: leftId,
        right_sheet_id: rightId,
        join_keys: [{ left_column: 'code', right_column: 'code' }],
        how: 'inner',
        target_sheet_name: 'People x Roles',
      },
      idempotency_key: `plugin-contract-detail-join:${projectId}`,
    },
  });
  const joinBody = (await joinRes.json()) as Record<string, unknown>;
  expect(joinRes.ok(), JSON.stringify(joinBody)).toBeTruthy();
  const edgeSheetId = Number(
    (joinBody.outputs as Array<Record<string, unknown>>).find((o) => o.kind === 'sheet')!
      .sheet_id,
  );
  await openProject(page, projectId, edgeSheetId);

  await page.getByTestId('view-switch-graph').click();
  await expect(page.getByTestId('graph-svg')).toBeVisible({ timeout: 20_000 });
  await page.locator('[data-testid^="graph-node-"]').first().click();
  await expect(page.getByTestId('entity-detail-shell')).toBeVisible({ timeout: 20_000 });
  const entityTabId = `entity-detail-tab-${`${generatedPluginId}.panel.entity`.replace(/[^a-zA-Z0-9]+/g, '-')}`;
  await page.getByTestId(entityTabId).click();
  const entityTab = page.getByTestId('contract-detail-entity-tab');
  await expect(entityTab).toBeVisible();
  await expect(entityTab).toHaveAttribute('data-schema', 'frisket.plugin_detail_context.v1');
  await expect(entityTab).toHaveAttribute('data-subject-kind', 'entity');

  // Source detail: create a source, open its detail drawer, click the plugin tab.
  // Source creation re-homed into the Import workspace feed mode (SourcesPanel
  // no longer owns a creation form — sources.spec.ts pins source-add-button
  // absent) and the panel itself lives in the Discover Sources tab
  // (workbench-ia-right-edge-v1; sidebar retired at inc 8).
  await openImportWorkspace(page);
  await page.getByTestId('import-mode-feed').click();
  await page.getByTestId('source-name').fill('Detail parity feed');
  await page.getByTestId('source-url').fill('http://localhost/feed.xml');
  await page.getByTestId('source-create').click();
  // Manual feed creation surfaces a "Populate feed?" prompt inside the still-
  // open import dialog (feed-add-populate-prompt-v1, pinned by
  // feed-add-populate-prompt.spec.ts). Dismiss it ("Not now" creates the
  // source without polling) so the modal closes before we reach the Discover
  // Sources tab — otherwise the still-open dialog intercepts the tab click.
  await page.getByTestId('feed-populate-dismiss').click();
  await expect(page.getByTestId('import-workspace-dialog')).toBeHidden();
  await openDiscoverTab(page, 'Sources');
  const sourcesRes = await page.request.get(`/api/projects/${projectId}/sources`);
  const sources = (await sourcesRes.json()) as Array<{ id: number; name: string }>;
  const source = sources.find((candidate) => candidate.name === 'Detail parity feed');
  expect(source).toBeTruthy();
  await page.getByTestId(`source-health-${source!.id}`).click();
  await expect(page.getByTestId('source-health-drawer')).toBeVisible();
  const sourceTabId = `source-detail-tab-${`${generatedPluginId}.panel.source`.replace(/[^a-zA-Z0-9]+/g, '-')}`;
  await page.getByTestId(sourceTabId).click();
  const sourceTab = page.getByTestId('contract-detail-source-tab');
  await expect(sourceTab).toBeVisible();
  await expect(sourceTab).toHaveAttribute('data-schema', 'frisket.plugin_detail_context.v1');
  await expect(sourceTab).toHaveAttribute('data-subject-kind', 'source');
  await expect(sourceTab).toHaveAttribute(
    'data-subject',
    JSON.stringify({ kind: 'source', sourceId: String(source!.id) }),
  );
});

test('generated plugin peek opens from its own palette command and the host owns dismissal', async ({
  page,
}, testInfo) => {
  const generatedPluginId = pluginId();
  const runSuffix = suffix();
  const peekId = `${generatedPluginId}.panel.peek`;
  const commandId = `${generatedPluginId}.command.open_peek`;
  const handlerExport = `OpenPeek${runSuffix}`;
  const peekExport = `PeekPanel${runSuffix}`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-peek'));
  await mkdir(join(pluginRoot, 'frontend'), { recursive: true });
  await writeFile(
    join(pluginRoot, 'plugin.json'),
    JSON.stringify({
      schema_version: 'frisket.plugin.v1',
      id: generatedPluginId,
      version: '0.1.0',
      contributes: {
        workbench_views: [],
        workbench_panels: [peekId],
        workbench_commands: [commandId],
        actions: [], importers: [], operators: [], projections: [], column_types: [], job_handlers: [],
      },
      requires: { capabilities: [], secrets: [] },
      runtime: {
        actions: [], importers: [], operators: [], projections: [], job_handlers: [],
        workbench_components: [
          { contribution_id: peekId, module_key: `${generatedPluginId}.ui`, component_key: `${generatedPluginId}.components.${peekExport}`, module_path: 'frontend/plugin.js' },
          { contribution_id: commandId, module_key: `${generatedPluginId}.ui`, component_key: `${generatedPluginId}.commands.${handlerExport}`, module_path: 'frontend/plugin.js' },
        ],
      },
    }, null, 2),
    'utf-8',
  );
  await writeFile(
    join(pluginRoot, 'workbench-descriptors.json'),
    JSON.stringify({
      schemaVersion: 'frisket.workbench_descriptor_package.v1',
      descriptors: [
        {
          schemaVersion: 'frisket.workbench.panel.v1',
          id: peekId,
          kind: 'panel',
          title: 'Contract Peek',
          ownerPluginId: generatedPluginId,
          componentKey: `${generatedPluginId}.components.${peekExport}`,
          placements: [{ host: 'modalOrPeek', mode: 'peek', slot: 'interruption', placementId: `${generatedPluginId}-peek`, order: 100 }],
          requires: [{ kind: 'hostCapability', id: 'sheet.active' }],
          dataRequirements: [{ kind: 'activeSheet' }],
        },
        {
          schemaVersion: 'frisket.command.v1',
          id: commandId,
          kind: 'command',
          title: 'Contract Open Peek',
          ownerPluginId: generatedPluginId,
          componentKey: `${generatedPluginId}.commands.${handlerExport}`,
          commandId,
          handlerKey: `${generatedPluginId}.commands.${handlerExport}`,
          placements: [{ host: 'commandPalette', mode: 'command', slot: 'launcher', placementId: `${generatedPluginId}-open-peek`, order: 95 }],
          requires: [],
          dataRequirements: [],
        },
      ],
    }, null, 2),
    'utf-8',
  );
  await writeFile(
    join(pluginRoot, 'frontend/plugin.js'),
    `
export const ${handlerExport} = ({ ctx }) => {
  // First try to open a FOREIGN contribution (must be rejected by the host),
  // then the plugin's own peek.
  ctx?.peek?.open('frisket.core.view.evidence');
  ctx?.peek?.open(${JSON.stringify(peekId)});
};

export const ${peekExport} = ({ React, ctx }) => {
  return React.createElement('section', {
    'data-testid': 'contract-plugin-peek',
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-can-close': String(typeof ctx?.peek?.close === 'function'),
  }, 'contract plugin peek');
};
`,
    'utf-8',
  );

  const projectId = await createProject(page.request, uniqueName('plugin-contract-peek'));
  const sheetId = await importCsv(
    page.request, projectId, 'people.csv', 'name,city\nAda,London\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  // User-gesture only: nothing opens on load.
  await expect(page.getByTestId('contract-plugin-peek')).toHaveCount(0);

  const runCommand = async () => {
    await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
    await page
      .getByTestId(`workbench-command-${commandId.replace(/[^a-zA-Z0-9]+/g, '-')}`)
      .click();
    await page.getByLabel('Close command palette').click();
  };

  await runCommand();
  const peek = page.getByTestId('contract-plugin-peek');
  await expect(peek).toBeVisible();
  await expect(peek).toHaveAttribute('data-schema', 'frisket.plugin_peek_context.v1');
  await expect(peek).toHaveAttribute('data-can-close', 'true');
  // The foreign open was rejected: the evidence viewer did NOT open, and the
  // host recorded the rejection.
  await expect(page.getByTestId('evidence-viewer')).toHaveCount(0);
  await expect(page.getByTestId('workbench-region-modalOrPeek')).toHaveAttribute(
    'data-plugin-peek-rejected',
    'frisket.core.view.evidence',
  );

  // Host-owned Escape unmounts (not hides).
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('contract-plugin-peek')).toHaveCount(0);
  await expect(page.getByTestId('plugin-peek-shell')).toHaveCount(0);

  // Reopen, then host-owned backdrop click unmounts. Click the lower backdrop
  // area: the top-left corner sits under the post-redesign chrome bar, so a
  // (4,4) click no longer reaches the backdrop (workbench-ia chrome).
  await runCommand();
  await expect(page.getByTestId('contract-plugin-peek')).toBeVisible();
  // Real pointer press on the exposed backdrop (left of the centered shell).
  // Regression guard: the contribution frame must stay contained in the shell
  // (styles.css .plugin-peek-shell exemption) or this click lands on an
  // invisible full-viewport layer instead of the backdrop.
  await page
    .getByTestId('plugin-peek-backdrop')
    .click({ position: { x: 12, y: 360 } });
  await expect(page.getByTestId('contract-plugin-peek')).toHaveCount(0);
});

// SHARED ACT HOST: a plugin
// launcher placement (ex-activityRail, still DECLARED as activityRail/command)
// ingests into the LIBRARY group in both Act densities AND the ⌘K palette. The
// launcher is host-owned (no plugin code runs to render it); clicking it reveals
// the descriptor's PRIMARY placement — here a bottomDock tab — through the
// integrity path.
test('generated plugin launcher is shared across Act densities and reveals its primary placement', async ({
  page,
}, testInfo) => {
  const generatedPluginId = pluginId();
  const runSuffix = suffix();
  const panelId = `${generatedPluginId}.panel.dock`;
  const viewId = `${generatedPluginId}.view.${suffix()}`;
  const panelExport = `DockTab${runSuffix}`;
  const viewExport = `View${runSuffix}`;
  const token = `contract-token-${runSuffix}`;
  const dockPlacementId = `${generatedPluginId}-dock`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-rail'));
  await writeRuntimeUiPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    viewId,
    panelId,
    viewExport,
    panelExport,
    token,
    panelPlacements: [
      {
        host: 'bottomDock',
        mode: 'tab',
        slot: 'companion.output',
        placementId: dockPlacementId,
        order: 105,
      },
      {
        host: 'activityRail',
        mode: 'command',
        slot: 'launcher',
        placementId: `${generatedPluginId}-rail`,
        order: 105,
      },
    ],
  });

  const projectId = await createProject(page.request, uniqueName('plugin-contract-rail'));
  const sheetId = await importCsv(
    page.request, projectId, 'people.csv', 'name,city\nAda,London\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  // The expanded Act LIBRARY group renders a host-owned launcher for the runtime
  // descriptor; the plugin component is NOT mounted (dock tab inactive, no code
  // ran). The Analyze tab (stable internal id `home`) is active by default, so
  // LIBRARY is visible.
  const launcherSlug = panelId.replace(/[^a-zA-Z0-9]+/g, '-');
  const launcher = page.getByTestId(`ribbon-launcher-${launcherSlug}`);
  await expect(launcher).toBeVisible();
  await expect(launcher).toHaveAttribute('data-runtime-source', 'runtimeIndex');
  await expect(launcher).toHaveAttribute('data-contribution-id', panelId);
  await expect(page.getByTestId('contract-plugin-panel')).toHaveCount(0);
  await expect(page.getByTestId('trusted-local-plugin-component-loading')).toHaveCount(0);

  // The launcher is also a ⌘K palette command, alongside both Act densities.
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  await expect(page.getByTestId('workbench-region-commandPalette')).toBeVisible();
  await expect(page.getByTestId(`workbench-launcher-command-${launcherSlug}`)).toBeVisible();
  await page.getByLabel('Close command palette').click();
  await expect(page.getByTestId('contract-plugin-panel')).toHaveCount(0);

  // The compact Act density renders the same host-owned launcher. Launching
  // from it selects the primary bottomDock tab, and only THEN does the plugin
  // component mount through the integrity path.
  await page.getByTestId('ribbon-collapse').click();
  await page.getByTestId('menubar-menu-home').click();
  const compactLauncher = page.getByTestId(`menu-launcher-${launcherSlug}`);
  await expect(compactLauncher).toBeVisible();
  await expect(compactLauncher).toHaveAttribute('data-runtime-source', 'runtimeIndex');
  await expect(compactLauncher).toHaveAttribute('data-contribution-id', panelId);
  await compactLauncher.click();
  await expect(page.getByTestId('bottom-dock-panel')).toHaveAttribute(
    'data-active-placement-id',
    dockPlacementId,
  );
  const panel = page.getByTestId('contract-plugin-panel');
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('data-schema', 'frisket.plugin_dock_tab_context.v1');
  await expect(panel).toHaveAttribute('data-token', token);
});

async function writeGridStatePluginPackage({
  root,
  pluginId,
  declaringId,
  plainId,
  handlerSuffix,
}: {
  root: string;
  pluginId: string;
  declaringId: string;
  plainId: string;
  handlerSuffix: string;
}) {
  const panels = [
    {
      id: declaringId,
      exportName: `Declaring${handlerSuffix}`,
      testId: 'contract-gridstate-declaring',
      requires: [
        { kind: 'hostCapability', id: 'sheet.active' },
        { kind: 'hostCapability', id: 'grid.state.read' },
        { kind: 'hostCapability', id: 'action.run' },
      ],
      order: 92,
    },
    {
      id: plainId,
      exportName: `Plain${handlerSuffix}`,
      testId: 'contract-gridstate-plain',
      requires: [{ kind: 'hostCapability', id: 'sheet.active' }],
      order: 93,
    },
  ];
  await mkdir(join(root, 'frontend'), { recursive: true });
  await writeFile(
    join(root, 'plugin.json'),
    JSON.stringify({
      schema_version: 'frisket.plugin.v1',
      id: pluginId,
      version: '0.1.0',
      contributes: {
        workbench_views: [],
        workbench_panels: panels.map((panel) => panel.id),
        actions: [], importers: [], operators: [], projections: [], column_types: [], job_handlers: [],
      },
      requires: { capabilities: [], secrets: [] },
      runtime: {
        actions: [], importers: [], operators: [], projections: [], job_handlers: [],
        workbench_components: panels.map((panel) => ({
          contribution_id: panel.id,
          module_key: `${pluginId}.ui`,
          component_key: `${pluginId}.components.${panel.exportName}`,
          module_path: 'frontend/plugin.js',
        })),
      },
    }, null, 2),
    'utf-8',
  );
  await writeFile(
    join(root, 'workbench-descriptors.json'),
    JSON.stringify({
      schemaVersion: 'frisket.workbench_descriptor_package.v1',
      descriptors: panels.map((panel) => ({
        schemaVersion: 'frisket.workbench.panel.v1',
        id: panel.id,
        kind: 'panel',
        title: `Contract ${panel.exportName}`,
        ownerPluginId: pluginId,
        componentKey: `${pluginId}.components.${panel.exportName}`,
        placements: [{ host: 'bottomDock', mode: 'tab', slot: 'companion.output', placementId: `${pluginId}-${panel.exportName}`, order: panel.order }],
        requires: panel.requires,
        dataRequirements: [{ kind: 'activeSheet' }],
      })),
    }, null, 2),
    'utf-8',
  );
  await writeFile(
    join(root, 'frontend/plugin.js'),
    panels
      .map(
        (panel) => `
export const ${panel.exportName} = ({ React, ctx }) =>
  React.createElement('aside', {
    'data-testid': ${JSON.stringify(panel.testId)},
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-has-grid-state': String(ctx?.gridState !== undefined),
    'data-grid-filter': JSON.stringify(ctx?.gridState?.filter ?? null),
    'data-grid-sort': JSON.stringify(ctx?.gridState?.sort ?? null),
    'data-visible-columns': String(ctx?.gridState?.visibleColumnIds?.length ?? -1),
    'data-frozen-count': String(ctx?.gridState?.frozenColumnCount ?? -1),
    'data-lens-id': String(ctx?.gridState?.activeLensId ?? 'none'),
    'data-has-actions': String(ctx?.actions !== undefined),
  }, 'contract grid state panel');
`,
      )
      .join('\n'),
    'utf-8',
  );
}

test('generated plugin contexts expose grid read state only when declared', async ({
  page,
}, testInfo) => {
  const generatedPluginId = pluginId();
  const handlerSuffix = suffix();
  const declaringId = `${generatedPluginId}.panel.declaring`;
  const plainId = `${generatedPluginId}.panel.plain`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-gridstate'));
  await writeGridStatePluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    declaringId,
    plainId,
    handlerSuffix,
  });

  const projectId = await createProject(page.request, uniqueName('plugin-contract-gridstate'));
  const sheetId = await importCsv(
    page.request,
    projectId,
    'tickets.csv',
    'status,title\nopen,First\nclosed,Second\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  const gridStateColumns = await sheetColumns(page.request, projectId, sheetId);
  await openProject(page, projectId, sheetId);

  // Apply a REAL grid filter so the declared context carries live state (the
  // generated contributions are dock tabs, so nothing obstructs the popup).
  await openFriendlyFilterSidebar(page, gridStateColumns, 'status');
  await page.getByTestId('facet-check-status-open').check();
  await expect(page.getByTestId('active-grid-filter')).toContainText('status');

  await page.getByTestId(`bottom-dock-tab-${generatedPluginId}-Declaring${handlerSuffix}`).click();
  const declaring = page.getByTestId('contract-gridstate-declaring');
  await expect(declaring).toBeVisible();
  await expect(declaring).toHaveAttribute('data-has-grid-state', 'true');
  await expect(declaring).toHaveAttribute('data-grid-filter', /status/);
  await expect(declaring).toHaveAttribute('data-grid-filter', /open/);
  await expect(declaring).toHaveAttribute('data-visible-columns', /^[1-9]/);
  await expect(declaring).toHaveAttribute('data-frozen-count', /^\d+$/);
  await expect(declaring).toHaveAttribute('data-has-actions', 'true');
  // Live SORT flows through too: sort by the status column and the declared
  // context reflects it.
  await openAdvancedSortPanel(page, gridStateColumns, 'status');
  await page.getByTestId('grid-sort-column').selectOption('status');
  await page.getByTestId('apply-grid-sort').click();
  await expect(declaring).toHaveAttribute('data-grid-sort', /status/);

  // The non-declaring panel gets NO grid read state and NO actions section.
  await page.getByTestId(`bottom-dock-tab-${generatedPluginId}-Plain${handlerSuffix}`).click();
  const plain = page.getByTestId('contract-gridstate-plain');
  await expect(plain).toBeVisible();
  await expect(plain).toHaveAttribute('data-has-grid-state', 'false');
  await expect(plain).toHaveAttribute('data-has-actions', 'false');
});

// NEW HOST: a plugin VIEW launcher
// ingests into the ribbon LIBRARY group; clicking it reveals the main view.
test('generated plugin view launcher reveals the main view with declared grid state', async ({
  page,
}, testInfo) => {
  const generatedPluginId = pluginId();
  const runSuffix = suffix();
  const viewId = `${generatedPluginId}.view.rail`;
  const panelId = `${generatedPluginId}.panel.unused`;
  const viewExport = `RailView${runSuffix}`;
  const panelExport = `Panel${runSuffix}`;
  const token = `contract-token-${runSuffix}`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-view-rail'));
  await writeRuntimeUiPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    viewId,
    panelId,
    viewExport,
    panelExport,
    token,
  });
  // Give the VIEW a secondary rail launcher placement.
  const descriptorsPath = join(pluginRoot, 'workbench-descriptors.json');
  const pkg = JSON.parse(await (await import('node:fs/promises')).readFile(descriptorsPath, 'utf-8'));
  pkg.descriptors[0].placements.push({
    host: 'activityRail',
    mode: 'command',
    slot: 'launcher',
    placementId: `${generatedPluginId}-view-rail`,
    order: 106,
  });
  pkg.descriptors[0].requires.push({ kind: 'hostCapability', id: 'grid.state.read' });
  await writeFile(descriptorsPath, JSON.stringify(pkg, null, 2), 'utf-8');

  const projectId = await createProject(page.request, uniqueName('plugin-contract-view-rail'));
  const sheetId = await importCsv(
    page.request, projectId, 'people.csv', 'name,city\nAda,London\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  const launcher = page.getByTestId(
    `ribbon-launcher-${viewId.replace(/[^a-zA-Z0-9]+/g, '-')}`,
  );
  await expect(launcher).toBeVisible();
  await expect(launcher).toHaveAttribute('data-runtime-source', 'runtimeIndex');
  await expect(launcher).toHaveAttribute('data-contribution-id', viewId);
  await launcher.click();
  // The view is revealed as the active main view and mounts with grid state
  // (the descriptor declares grid.state.read above).
  const view = page.getByTestId('contract-plugin-main-view');
  await expect(view).toBeVisible();
  await expect(view).toHaveAttribute('data-schema', 'frisket.plugin_view_context.v1');
  await expect(view).toHaveAttribute('data-has-grid-state', 'true');
  await expect(view).toHaveAttribute('data-grid-visible-columns', /^[1-9]/);
});

test('generated plugin action launch is host-mediated behind a real cost gate', async ({
  page,
}, testInfo) => {
  const generatedPluginId = pluginId();
  const handlerSuffix = suffix();
  const declaringId = `${generatedPluginId}.panel.declaring`;
  const plainId = `${generatedPluginId}.panel.plain`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-actionlaunch'));
  await writeGridStatePluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    declaringId,
    plainId,
    handlerSuffix,
  });
  // Give the declaring panel a launch button wired to ctx.actions.run.
  const modulePath = join(pluginRoot, 'frontend/plugin.js');
  const fs = await import('node:fs/promises');
  let moduleSource = await fs.readFile(modulePath, 'utf-8');
  moduleSource = moduleSource.replace(
    "}, 'contract grid state panel');",
    `}, React.createElement('button', {
    type: 'button',
    'data-testid': 'contract-launch-action',
    onClick: async () => {
      const outcome = await ctx?.actions?.run('classify.rows');
      document.body.dataset.pluginActionLaunched = String(outcome?.launched ?? 'missing');
    },
  }, 'launch action'));`,
  );
  await fs.writeFile(modulePath, moduleSource, 'utf-8');

  const projectId = await createProject(page.request, uniqueName('plugin-contract-actionlaunch'));
  const sheetId = await importCsv(
    page.request, projectId, 'people.csv', 'name,city\nAda,London\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  await page.getByTestId(`bottom-dock-tab-${generatedPluginId}-Declaring${handlerSuffix}`).click();
  const launch = page.getByTestId('contract-launch-action').first();
  await expect(launch).toBeVisible();

  // 1) The REAL cost gate appears BEFORE anything else; cancel -> the route
  //    never gains the action prefill, no run, {launched:false}.
  await launch.click();
  await expect(page.getByTestId('cost-gate-estimate')).toBeVisible();
  await page.getByTestId('cost-gate-cancel').click();
  await expect(page.getByTestId('cost-gate-estimate')).toHaveCount(0);
  await expect(page.locator('body')).toHaveAttribute('data-plugin-action-launched', 'false');
  expect(page.url()).not.toContain('/action/');

  // 2) Confirm -> the action panel opens PREFILLED (the action route carries
  //    the kind); still NO run without the user acting in the panel.
  await launch.click();
  await expect(page.getByTestId('cost-gate-estimate')).toBeVisible();
  await page.getByTestId('cost-gate-input').fill('confirm');
  await page.getByTestId('cost-gate-confirm').click();
  await expect(page.locator('body')).toHaveAttribute('data-plugin-action-launched', 'true');
  await expect(page).toHaveURL(/\/action\/classify\.rows/);
  await expect(page.getByTestId('action-panel')).toBeVisible();
  const jobs = await page.request.get(`/api/projects/${projectId}/actions/jobs`);
  expect(((await jobs.json()) as { jobs?: unknown[] }).jobs ?? []).toHaveLength(0);
});

test('sdk-built plugin package mounts through the workbench contract', async ({
  page,
}, testInfo) => {
  const { execFileSync } = await import('node:child_process');
  const repoRoot = resolve(import.meta.dirname, '../../..');
  const generatedPluginId = pluginId();
  const pluginRoot = resolve(testInfo.outputPath('sdk-built-plugin'));

  // The REAL authoring pipeline: init (SDK source only) -> build (emits and
  // validates the loader artifacts). No hand-written plugin.json anywhere.
  execFileSync(
    'uv',
    [
      'run', 'frisket', 'plugin', 'init',
      '--id', generatedPluginId,
      '--output', pluginRoot,
      '--with', 'panel', '--with', 'command',
    ],
    { cwd: repoRoot, stdio: 'pipe' },
  );
  execFileSync('uv', ['run', 'frisket', 'plugin', 'build', pluginRoot], {
    cwd: repoRoot,
    stdio: 'pipe',
  });

  const projectId = await createProject(page.request, uniqueName('plugin-contract-sdk'));
  const sheetId = await importCsv(
    page.request, projectId, 'people.csv', 'name,city\nAda,London\n',
  );
  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  // The SDK-templated inspector panel mounts through the integrity path with
  // the real v1 panel context.
  const shell = page.getByTestId(
    `workbench-contribution-${`${generatedPluginId}.panel.inspector`.replace(/[^a-zA-Z0-9]+/g, '-')}`,
  );
  await expect(shell).toBeVisible();
  await expect(shell).toHaveAttribute('data-plugin-panel-status', 'mounted');
  const panel = shell.getByTestId('plugin-inspector-panel');
  await expect(panel).toBeVisible();
  await expect(panel).toHaveAttribute('data-schema', 'frisket.plugin_panel_context.v1');

  // And the SDK-templated palette command registered and runs on click.
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+Shift+P' : 'Control+Shift+P');
  await page
    .getByTestId(
      `workbench-command-${`${generatedPluginId}.command.hello`.replace(/[^a-zA-Z0-9]+/g, '-')}`,
    )
    .click();
  await expect(page.locator('body')).toHaveAttribute(
    'data-plugin-command-ran',
    `${generatedPluginId}.command.hello`,
  );
});

test('sdk-built plugin view receives live selection', async ({
  page,
}, testInfo) => {
  const { execFileSync } = await import('node:child_process');
  const repoRoot = resolve(import.meta.dirname, '../../..');

  // Part 1: a plain SDK-built mainView view (frisket plugin init --with view
  // -> build, the real authoring pipeline) gets the WORKSPACE's live row
  // selection — the same state panel-family contexts already receive. The
  // mainView placement mounts as a companion pane alongside the grid
  // (App.tsx companionMainViewDescriptor), so the grid stays reachable and
  // selecting rows there must live-update ctx.selection on the mounted view.
  const generatedPluginId = pluginId();
  const pluginRoot = resolve(testInfo.outputPath('sdk-built-plugin-view-selection'));
  execFileSync(
    'uv',
    [
      'run', 'frisket', 'plugin', 'init',
      '--id', generatedPluginId,
      '--output', pluginRoot,
      '--with', 'view',
    ],
    { cwd: repoRoot, stdio: 'pipe' },
  );
  execFileSync('uv', ['run', 'frisket', 'plugin', 'build', pluginRoot], {
    cwd: repoRoot,
    stdio: 'pipe',
  });

  const projectId = await createProject(page.request, uniqueName('plugin-contract-view-selection'));
  const sheetId = await importCsv(
    page.request,
    projectId,
    'people.csv',
    'name,city\nAda,London\nGrace,Arlington\nLinus,Helsinki\n',
  );
  const data = await sheetData(page.request, projectId, sheetId, 0, 3);
  const firstRowId = String(data.rows[0].id);
  const secondRowId = String(data.rows[1].id);

  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  const view = page.getByTestId('plugin-main-view');
  await expect(view).toBeVisible();
  await expect(view).toHaveAttribute('data-schema', 'frisket.plugin_view_context.v1');
  await expect(view).toHaveAttribute('data-selected-count', '0');
  await expect(view).toHaveAttribute('data-selected-row-ids', '');

  await selectRow(page, 0);
  await expect(view).toHaveAttribute('data-selected-count', '1');
  await expect(view).toHaveAttribute('data-selected-row-ids', firstRowId);

  await selectRow(page, 1);
  await expect(view).toHaveAttribute('data-selected-count', '2');
  await expect(view).toHaveAttribute('data-selected-row-ids', `${firstRowId},${secondRowId}`);

  // Deselect the first row: count and id list both update live — this is
  // NOT a snapshot captured at mount time.
  await selectRow(page, 0);
  await expect(view).toHaveAttribute('data-selected-count', '1');
  await expect(view).toHaveAttribute('data-selected-row-ids', secondRowId);

  // Part 2: a projection view gets the section too (M3 scope covers both
  // view kinds, not just the plain mainView view) — a fresh project avoids
  // competing with the Part 1 view for the single companion mainView slot.
  const projectionPluginId = pluginId();
  const projectionRunSuffix = suffix();
  const projectionViewId = `${projectionPluginId}.view.${suffix()}`;
  const projectionViewExport = `SelectionProjectionView${projectionRunSuffix}`;
  const projectionKind = `${projectionPluginId}.projection.${suffix()}`;
  const projectionToken = `contract-token-${projectionRunSuffix}`;
  const projectionRoot = resolve(testInfo.outputPath('generated-projection-view-selection'));
  await writeProjectionViewPluginPackage({
    root: projectionRoot,
    pluginId: projectionPluginId,
    viewId: projectionViewId,
    viewExport: projectionViewExport,
    projectionKind,
    token: projectionToken,
  });

  const projectionProjectId = await createProject(
    page.request,
    uniqueName('plugin-contract-projection-view-selection'),
  );
  const projectionSheetId = await importCsv(
    page.request,
    projectionProjectId,
    'events.csv',
    'name,happened\nFiling,2020-01-02\nHearing,2020-03-04\n',
  );
  const projectionColumns = await sheetColumns(page.request, projectionProjectId, projectionSheetId);
  const dateColumn = projectionColumns.find((column) => column.name === 'happened');
  expect(dateColumn).toBeTruthy();
  await setColumnType(page.request, projectionProjectId, dateColumn!.id, 'date');
  const projectionData = await sheetData(page.request, projectionProjectId, projectionSheetId, 0, 2);
  const projectionFirstRowId = String(projectionData.rows[0].id);

  await installAndActivatePlugin(
    page.request,
    projectionProjectId,
    projectionPluginId,
    projectionRoot,
  );
  await openProject(page, projectionProjectId, projectionSheetId);

  const projectionView = page.getByTestId('contract-plugin-projection-view');
  await expect(projectionView).toBeVisible();
  await expect(projectionView).toHaveAttribute(
    'data-schema',
    'frisket.plugin_projection_view_context.v1',
  );
  await expect(projectionView).toHaveAttribute('data-selected-count', '0');

  await selectRow(page, 0);
  await expect(projectionView).toHaveAttribute('data-selected-count', '1');
  await expect(projectionView).toHaveAttribute('data-selected-row-ids', projectionFirstRowId);
});

async function writeMapCapabilitiesPluginPackage({
  root,
  pluginId,
  viewId,
  viewExport,
  token,
}: {
  root: string;
  pluginId: string;
  viewId: string;
  viewExport: string;
  token: string;
}) {
  await mkdir(join(root, 'frontend'), { recursive: true });
  await writeFile(
    join(root, 'plugin.json'),
    JSON.stringify(
      {
        schema_version: 'frisket.plugin.v1',
        id: pluginId,
        version: '0.1.0',
        contributes: {
          workbench_views: [viewId],
          workbench_panels: [],
          actions: [],
          importers: [],
          operators: [],
          projections: [],
          column_types: [],
          job_handlers: [],
        },
        requires: {
          capabilities: [],
          secrets: [],
        },
        runtime: {
          actions: [],
          importers: [],
          operators: [],
          projections: [],
          job_handlers: [],
          workbench_components: [
            {
              contribution_id: viewId,
              module_key: `${pluginId}.ui`,
              component_key: `${pluginId}.components.${viewExport}`,
              module_path: 'frontend/plugin.js',
            },
          ],
        },
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'workbench-descriptors.json'),
    JSON.stringify(
      {
        schemaVersion: 'frisket.workbench_descriptor_package.v1',
        descriptors: [
          {
            schemaVersion: 'frisket.workbench.view.v1',
            id: viewId,
            kind: 'view',
            title: 'Contract Map View',
            ownerPluginId: pluginId,
            componentKey: `${pluginId}.components.${viewExport}`,
            placements: [
              {
                host: 'mainView',
                mode: 'pane',
                slot: 'work.companion',
                placementId: `${pluginId}-map-view`,
                order: 91,
              },
            ],
            // The two capabilities MAP_VIEW_DESCRIPTOR names
            // (web/src/workbench/descriptors.ts:729-746) that a runtime
            // plugin mainView actually exercises.
            requires: [
              { kind: 'hostCapability', id: 'sheet.rows.read' },
              { kind: 'hostCapability', id: 'host.navigation.openRow' },
              { kind: 'hostCapability', id: 'grid.filter.applyBbox' },
            ],
            dataRequirements: [
              { kind: 'activeSheet' },
              { kind: 'sheetHasColumnType', columnType: 'geo_point' },
            ],
          },
        ],
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'frontend/plugin.js'),
    `
// Continental-US bbox from tests/engine/test_geo_bbox_filter.py's canonical fixture
// (selects NYC + LA, excludes London) — reused verbatim so the applied
// filter matches the pinned Python contract byte-for-byte.
const CONTINENTAL_US_BBOX = [-125.0, 24.0, -66.0, 50.0];

export const ${viewExport} = ({ React, ctx }) => {
  const [state, setState] = React.useState({ status: 'booting', firstRowId: '', geoColumnId: '' });
  React.useEffect(() => {
    let active = true;
    if (!ctx || ctx.schemaVersion !== 'frisket.plugin_view_context.v1') {
      setState({ status: 'missing-context', firstRowId: '', geoColumnId: '' });
      return () => { active = false; };
    }
    const geoColumn = (ctx.sheet?.columns ?? []).find((column) => column.type === 'geo_point');
    ctx.rows.query({ offset: 0, limit: 1 }).then((page) => {
      if (!active) return;
      setState({
        status: 'ready',
        firstRowId: String(page.rows[0]?.id ?? ''),
        geoColumnId: geoColumn ? geoColumn.id : '',
      });
    }).catch((error) => {
      if (!active) return;
      setState({ status: 'error:' + String(error?.message ?? error), firstRowId: '', geoColumnId: '' });
    });
    return () => { active = false; };
  }, [ctx]);
  return React.createElement('section', {
    'data-testid': 'contract-plugin-map-view',
    'data-token': ${JSON.stringify(token)},
    'data-status': state.status,
    'data-schema': ctx?.schemaVersion ?? 'missing',
    'data-can-open-row': String(typeof ctx?.navigation?.openRow === 'function'),
    'data-can-apply-bbox': String(typeof ctx?.gridFilter?.applyBbox === 'function'),
  },
    React.createElement('button', {
      type: 'button',
      'data-testid': 'contract-plugin-map-open-row',
      onClick: () => state.firstRowId && ctx?.navigation?.openRow(state.firstRowId),
    }, 'open first row'),
    React.createElement('button', {
      type: 'button',
      'data-testid': 'contract-plugin-map-apply-bbox',
      onClick: () => state.geoColumnId && ctx?.gridFilter?.applyBbox(state.geoColumnId, CONTINENTAL_US_BBOX),
    }, 'filter to this area'),
  );
};
`,
    'utf-8',
  );
}

test('generated plugin view opens rows and applies bbox filters through host capabilities', async ({
  page,
}, testInfo) => {
  const generatedPluginId = pluginId();
  const runSuffix = suffix();
  const viewId = `${generatedPluginId}.view.map`;
  const viewExport = `MapView${runSuffix}`;
  const token = `contract-token-${runSuffix}`;
  const pluginRoot = resolve(testInfo.outputPath('generated-plugin-map-capabilities'));
  await writeMapCapabilitiesPluginPackage({
    root: pluginRoot,
    pluginId: generatedPluginId,
    viewId,
    viewExport,
    token,
  });

  const projectId = await createProject(page.request, uniqueName('plugin-contract-map-caps'));
  const sheetId = await importCsv(
    page.request,
    projectId,
    'places.csv',
    'name,point\nNYC,\nLA,\nLondon,\n',
  );
  const columns = await sheetColumns(page.request, projectId, sheetId);
  const point = columns.find((c) => c.name === 'point')!;
  await setColumnType(page.request, projectId, point.id, 'geo_point');
  const data = await sheetData(page.request, projectId, sheetId, 0, 3);
  const rows = data.rows as { id: number; cells: Record<string, unknown> }[];
  const nameColumnId = columns.find((c) => c.name === 'name')!.id;
  const coordsByName: Record<string, { lat: number; lon: number }> = {
    NYC: { lat: 40.7128, lon: -74.006 },
    LA: { lat: 34.0522, lon: -118.2437 },
    London: { lat: 51.5074, lon: -0.1278 },
  };
  await editCells(
    page.request,
    projectId,
    rows.map((row) => ({
      rowId: row.id,
      columnId: point.id,
      value: coordsByName[String(row.cells[String(nameColumnId)])],
    })),
  );
  const firstRow = rows[0];
  const firstRowName = String(firstRow.cells[String(nameColumnId)]);

  await installAndActivatePlugin(page.request, projectId, generatedPluginId, pluginRoot);
  await openProject(page, projectId, sheetId);

  const view = page.getByTestId('contract-plugin-map-view');
  await expect(view).toBeVisible();
  await expect(view).toHaveAttribute('data-token', token);
  await expect(view).toHaveAttribute('data-schema', 'frisket.plugin_view_context.v1');
  await expect(view).toHaveAttribute('data-status', 'ready');
  await expect(view).toHaveAttribute('data-can-open-row', 'true');
  await expect(view).toHaveAttribute('data-can-apply-bbox', 'true');

  // (a) host.navigation.openRow: the row drawer opens on the stable row id
  // the plugin fetched through ctx.rows.query.
  await view.getByTestId('contract-plugin-map-open-row').click();
  const rowDrawer = page.getByTestId('row-drawer');
  await expect(rowDrawer).toBeVisible();
  await expect(rowDrawer.getByTestId('row-field-name')).toContainText(firstRowName);
  await page.keyboard.press('Escape');
  await expect(rowDrawer).toBeHidden();

  // (b) grid.filter.applyBbox: the canonical {geo_col:{bbox}} filter
  // (tests/engine/test_geo_bbox_filter.py) reaches the /data re-fetch byte-for-byte
  // and the grid filter chip renders it — the same contract the first-party
  // MapView's "filter to this area" applies (App.tsx applyGridBboxFilter).
  const dataResp = page.waitForResponse((res) => {
    if (res.request().method() !== 'GET') return false;
    const url = new URL(res.url());
    return (
      url.pathname === `/api/projects/${projectId}/sheets/${sheetId}/data` &&
      (url.searchParams.get('filter') ?? '').includes('bbox')
    );
  });
  await view.getByTestId('contract-plugin-map-apply-bbox').click();
  const filteredResp = await dataResp;
  const filteredUrl = new URL(filteredResp.url());
  const appliedFilter = JSON.parse(filteredUrl.searchParams.get('filter') ?? 'null');
  expect(appliedFilter).toEqual({
    point: {
      bbox: { min_lon: -125.0, min_lat: 24.0, max_lon: -66.0, max_lat: 50.0 },
    },
  });
  const filtered = await filteredResp.json();
  expect(filtered.total).toBe(2);

  await expect(page.getByTestId('active-grid-filter')).toContainText('point in map area');
});

// plugin-host-library-injection-v1: a mainView descriptor declaring
// hostCapability host.library.deckgl receives ctx.libs.deckgl (a Promise for
// the host-injected deck.gl namespace); a sibling descriptor that does NOT
// declare it gets no ctx.libs at all — fail-closed by default, like every
// other capability-gated section. The SAME component export is reused for
// both descriptors: whether ctx.libs is present is entirely host-controlled
// (the fixture module never imports deck.gl itself — it can't; deck.gl isn't
// served to plugin modules, that's the whole point of this capability).
async function writeHostLibraryDeckglPluginPackage({
  root,
  pluginId,
  viewId,
  viewExport,
  token,
  declareCapability,
}: {
  root: string;
  pluginId: string;
  viewId: string;
  viewExport: string;
  token: string;
  declareCapability: boolean;
}) {
  await mkdir(join(root, 'frontend'), { recursive: true });
  await writeFile(
    join(root, 'plugin.json'),
    JSON.stringify(
      {
        schema_version: 'frisket.plugin.v1',
        id: pluginId,
        version: '0.1.0',
        contributes: {
          workbench_views: [viewId],
          workbench_panels: [],
          actions: [],
          importers: [],
          operators: [],
          projections: [],
          column_types: [],
          job_handlers: [],
        },
        requires: {
          capabilities: [],
          secrets: [],
        },
        runtime: {
          actions: [],
          importers: [],
          operators: [],
          projections: [],
          job_handlers: [],
          workbench_components: [
            {
              contribution_id: viewId,
              module_key: `${pluginId}.ui`,
              component_key: `${pluginId}.components.${viewExport}`,
              module_path: 'frontend/plugin.js',
            },
          ],
        },
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'workbench-descriptors.json'),
    JSON.stringify(
      {
        schemaVersion: 'frisket.workbench_descriptor_package.v1',
        descriptors: [
          {
            schemaVersion: 'frisket.workbench.view.v1',
            id: viewId,
            kind: 'view',
            title: 'Contract Deckgl View',
            ownerPluginId: pluginId,
            componentKey: `${pluginId}.components.${viewExport}`,
            placements: [
              {
                host: 'mainView',
                mode: 'pane',
                slot: 'work.companion',
                placementId: `${pluginId}-deckgl-view`,
                order: 91,
              },
            ],
            requires: [
              { kind: 'hostCapability', id: 'sheet.rows.read' },
              { kind: 'hostCapability', id: 'host.navigation.openRow' },
              ...(declareCapability
                ? [{ kind: 'hostCapability', id: 'host.library.deckgl' }]
                : []),
            ],
            dataRequirements: [{ kind: 'activeSheet' }],
          },
        ],
      },
      null,
      2,
    ),
    'utf-8',
  );
  await writeFile(
    join(root, 'frontend/plugin.js'),
    `
export const ${viewExport} = ({ React, ctx }) => {
  const canvasRef = React.useRef(null);
  const [state, setState] = React.useState({
    status: 'booting',
    hasLibs: 'unknown',
    deckLoaded: false,
    hasWebglContext: false,
    hasSetProps: false,
  });
  React.useEffect(() => {
    let active = true;
    let deckInstance = null;
    if (!ctx || ctx.schemaVersion !== 'frisket.plugin_view_context.v1') {
      setState((prev) => ({ ...prev, status: 'missing-context' }));
      return () => { active = false; };
    }
    if (!ctx.libs || !ctx.libs.deckgl) {
      // Fail-closed: no declaration -> no section at all, not a rejected
      // promise or a stubbed namespace.
      setState((prev) => ({ ...prev, status: 'no-libs', hasLibs: 'false' }));
      return () => { active = false; };
    }
    setState((prev) => ({ ...prev, hasLibs: 'true' }));
    // ctx.libs.deckgl is a Promise for the real namespace — the plugin
    // module never imports deck.gl itself (it can't; it isn't served to
    // plugin modules). This mirrors how the module must already await its
    // own TrustedLocalPluginComponent module load before rendering anything
    // real.
    ctx.libs.deckgl.then((deckgl) => {
      if (!active || !canvasRef.current) return;
      // A REAL Deck instance on a REAL canvas — not a stub. onLoad fires
      // once genuine initialization completes (deterministic event, not a
      // timing race or a pixel/screenshot assertion), and the canvas having
      // actually acquired a WebGL2 context is a second, independent
      // non-vacuous signal that Deck really drove this canvas.
      deckInstance = new deckgl.Deck({
        canvas: canvasRef.current,
        width: 200,
        height: 200,
        initialViewState: { longitude: 0, latitude: 0, zoom: 1 },
        controller: false,
        layers: [],
        onLoad: () => {
          if (!active) return;
          const hasWebglContext = Boolean(
            canvasRef.current && canvasRef.current.getContext('webgl2'),
          );
          setState((prev) => ({
            ...prev,
            status: 'deck-loaded',
            deckLoaded: true,
            hasWebglContext,
            hasSetProps: typeof deckInstance.setProps === 'function',
          }));
        },
      });
    }).catch((error) => {
      if (!active) return;
      setState((prev) => ({ ...prev, status: 'error:' + String(error?.message ?? error) }));
    });
    return () => {
      active = false;
      if (deckInstance) deckInstance.finalize();
    };
  }, [ctx]);
  return React.createElement('div', {
    'data-testid': 'contract-plugin-deckgl-view',
    'data-token': ${JSON.stringify(token)},
    'data-status': state.status,
    'data-has-libs': state.hasLibs,
    'data-deck-loaded': String(state.deckLoaded),
    'data-has-webgl-context': String(state.hasWebglContext),
    'data-has-set-props': String(state.hasSetProps),
    // Explicit pixel dimensions: Deck injects its own CSS onto the canvas
    // (and a sibling widget-overlay container) that can otherwise collapse
    // an auto-height parent to zero, which would make this element
    // "hidden" per Playwright's visibility check even though Deck genuinely
    // initialized.
    style: { position: 'relative', width: '200px', height: '200px' },
  }, React.createElement('canvas', {
    ref: canvasRef,
    width: 200,
    height: 200,
    'data-testid': 'contract-plugin-deckgl-canvas',
  }));
};
`,
    'utf-8',
  );
}

test('generated plugin view receives the host-injected deck.gl namespace', async ({
  page,
}, testInfo) => {
  const runSuffix = suffix();
  const token = `contract-token-${runSuffix}`;

  // (a) declaring host.library.deckgl: ctx.libs.deckgl resolves to the real
  // deck.gl namespace and a REAL Deck instance initializes on the canvas.
  const withLibsPluginId = pluginId();
  const withLibsViewId = `${withLibsPluginId}.view.deckgl`;
  const withLibsViewExport = `DeckglView${runSuffix}`;
  const withLibsRoot = resolve(testInfo.outputPath('generated-plugin-deckgl-with-libs'));
  await writeHostLibraryDeckglPluginPackage({
    root: withLibsRoot,
    pluginId: withLibsPluginId,
    viewId: withLibsViewId,
    viewExport: withLibsViewExport,
    token,
    declareCapability: true,
  });
  const withLibsProjectId = await createProject(
    page.request,
    uniqueName('plugin-contract-deckgl-with-libs'),
  );
  const withLibsSheetId = await importCsv(
    page.request, withLibsProjectId, 'people.csv', 'name,city\nAda,London\n',
  );
  await installAndActivatePlugin(page.request, withLibsProjectId, withLibsPluginId, withLibsRoot);
  await openProject(page, withLibsProjectId, withLibsSheetId);

  const withLibsView = page.getByTestId('contract-plugin-deckgl-view');
  await expect(withLibsView).toBeVisible();
  await expect(withLibsView).toHaveAttribute('data-token', token);
  await expect(withLibsView).toHaveAttribute('data-has-libs', 'true');
  // Non-vacuous: Deck's onLoad fired (real initialization completed) AND
  // the canvas actually acquired a WebGL2 rendering context from the Deck
  // instance — two independent, deterministic signals, neither a
  // screenshot/pixel assertion.
  await expect(withLibsView).toHaveAttribute('data-status', 'deck-loaded', { timeout: 15_000 });
  await expect(withLibsView).toHaveAttribute('data-deck-loaded', 'true');
  await expect(withLibsView).toHaveAttribute('data-has-webgl-context', 'true');
  await expect(withLibsView).toHaveAttribute('data-has-set-props', 'true');

  // (b) the sibling view WITHOUT the declaration gets no ctx.libs at all —
  // fail-closed by default, like every other gated section (a fresh project
  // + plugin install so this is a genuinely separate mount, not a leftover
  // state read from case (a)).
  const withoutLibsPluginId = pluginId();
  const withoutLibsViewId = `${withoutLibsPluginId}.view.deckgl`;
  const withoutLibsViewExport = `DeckglView${runSuffix}`;
  const withoutLibsRoot = resolve(testInfo.outputPath('generated-plugin-deckgl-without-libs'));
  await writeHostLibraryDeckglPluginPackage({
    root: withoutLibsRoot,
    pluginId: withoutLibsPluginId,
    viewId: withoutLibsViewId,
    viewExport: withoutLibsViewExport,
    token,
    declareCapability: false,
  });
  const withoutLibsProjectId = await createProject(
    page.request,
    uniqueName('plugin-contract-deckgl-without-libs'),
  );
  const withoutLibsSheetId = await importCsv(
    page.request, withoutLibsProjectId, 'people.csv', 'name,city\nAda,London\n',
  );
  await installAndActivatePlugin(
    page.request, withoutLibsProjectId, withoutLibsPluginId, withoutLibsRoot,
  );
  await openProject(page, withoutLibsProjectId, withoutLibsSheetId);

  const withoutLibsView = page.getByTestId('contract-plugin-deckgl-view');
  await expect(withoutLibsView).toBeVisible();
  await expect(withoutLibsView).toHaveAttribute('data-token', token);
  await expect(withoutLibsView).toHaveAttribute('data-status', 'no-libs');
  await expect(withoutLibsView).toHaveAttribute('data-has-libs', 'false');
  await expect(withoutLibsView).toHaveAttribute('data-deck-loaded', 'false');
});

test('bundled geo plugin map view mounts through the runtime index with live points', async ({
  page,
}) => {
  // The map is not first-party UI anymore. A FRESH project gets the
  // bundled frisket.geo plugin seeded through the public bundled install path
  // (src/frisket/authoring/bundled_plugins/frisket.geo/ — no install step in this
  // test, that IS the point), the runtime index carries its map view with an
  // integrity-served moduleUrl, and opening the map mounts the plugin module
  // through the generic PluginMainViewHost with LIVE Arrow points.
  await mockBasemapTiles(page);
  const projectId = await createProject(
    page.request,
    uniqueName('plugin-contract-bundled-geo-map'),
  );

  const index = await page.request.get(`/api/projects/${projectId}/workbench/plugins`);
  expect(index.ok()).toBeTruthy();
  const indexBody = (await index.json()) as {
    plugins: Array<{
      pluginId: string;
      installState: string;
      source?: { kind?: string; value?: string };
      frontendComponentBindings?: Array<{ contributionId: string; moduleUrl?: string }>;
    }>;
  };
  const geo = indexBody.plugins.find((plugin) => plugin.pluginId === 'frisket.geo');
  expect(geo).toBeTruthy();
  expect(geo!.installState).toBe('enabled');
  expect(geo!.source).toEqual({ kind: 'bundled', value: 'frisket.geo' });
  const mapBinding = geo!.frontendComponentBindings?.find(
    (binding) => binding.contributionId === 'frisket.geo.view.map',
  );
  expect(mapBinding?.moduleUrl).toBeTruthy();

  const sheetId = await importCsv(
    page.request,
    projectId,
    'places.csv',
    'place,point\n"NYC",\n"LA",\n',
  );
  const columns = await sheetColumns(page.request, projectId, sheetId);
  const point = columns.find((column) => column.name === 'point')!;
  const data = await sheetData(page.request, projectId, sheetId, 0, 2);
  const rows = data.rows as { id: number }[];
  await setColumnType(page.request, projectId, point.id, 'geo_point');
  await editCells(page.request, projectId, [
    { rowId: rows[0].id, columnId: point.id, value: { lat: 40.7128, lon: -74.006 } },
    { rowId: rows[1].id, columnId: point.id, value: { lat: 34.0522, lon: -118.2437 } },
  ]);

  await page.goto(`/p/${projectId}/s/${sheetId}/map/column/${point.id}`);

  const frame = page.getByTestId('workbench-contribution-frisket-geo-view-map');
  await expect(frame).toBeVisible();
  await expect(frame).toHaveAttribute('data-plugin-view-status', 'mounted');
  await expect(frame).toHaveAttribute('data-plugin-runtime-module-url', mapBinding!.moduleUrl!);
  await expect(frame).toHaveAttribute(
    'data-plugin-projection-kind',
    'frisket.geo.projection.map_points',
  );
  // The module executed with LIVE points through ctx.projection.fetchData.
  await expect(page.getByTestId('map-view')).toBeVisible();
  await expect(page.getByTestId('map-points-count')).toHaveText('2 points');
});

test('first-party descriptors resolve through the runtime descriptor pipeline', async ({
  page,
}) => {
  // First-party descriptor DATA is a
  // checked-in JSON package (src/frisket/data/
  // first_party_workbench_descriptors.json) validated by the SAME loader
  // plugin packages go through, served through the runtime index as an
  // honest `firstParty` section (no invented installState/receipts/
  // moduleUrl), and rendered by descriptors DERIVED from that artifact
  // (web/src/workbench/descriptors.ts) whose componentKey comes back from
  // the in-bundle registry (web/src/workbench/firstPartyComponents.ts) —
  // not a hardcoded literal. This proves the whole pipeline end to end.
  const projectId = await createProject(
    page.request,
    uniqueName('plugin-contract-first-party-descriptors'),
  );

  const index = await page.request.get(`/api/projects/${projectId}/workbench/plugins`);
  expect(index.ok()).toBeTruthy();
  const indexBody = (await index.json()) as {
    firstParty: {
      schemaVersion: string;
      descriptors: Array<{ id: string; [key: string]: unknown }>;
    };
    plugins: unknown[];
    loadedPluginCount: number;
  };
  expect(indexBody.firstParty.schemaVersion).toBe('frisket.workbench_descriptor_package.v1');
  // Honest section: no invented lifecycle rows for first-party ids inside
  // firstParty.descriptors itself (bundled plugins, e.g. frisket.geo, may
  // still be auto-enabled at bootstrap — that is the separate `plugins`
  // section, untouched by this addition).
  for (const descriptor of indexBody.firstParty.descriptors) {
    expect(descriptor).not.toHaveProperty('installState');
    expect(descriptor).not.toHaveProperty('receiptId');
    expect(descriptor).not.toHaveProperty('moduleUrl');
  }

  const sources = indexBody.firstParty.descriptors.find(
    (descriptor) => descriptor.id === 'frisket.core.panel.sources',
  );
  expect(sources).toBeTruthy();
  // Runtime-only fields are deliberately absent from the served artifact —
  // binding is a frontend registry lookup, not served data.
  expect(sources).not.toHaveProperty('componentKey');
  expect(sources).not.toHaveProperty('moduleUrl');

  const sheetId = await importCsv(page.request, projectId, 'places.csv', 'place\n"Pier 57"\n');
  await openProject(page, projectId, sheetId);

  // The Sources panel is descriptor #1 among first-party contributions
  // (no async flash — the frontend parses first-party from the
  // statically-imported artifact, not a network round trip) and its
  // componentKey/host/slot are DERIVED from the artifact + registry, not
  // hand-authored literals. Post-redesign the leftSidebar-declared panel
  // RENDERS inside the Discover panel's Sources tab (workbench-ia-right-edge-
  // v1 re-mapping; declaration unchanged), so reveal that tab first.
  await openDiscoverTab(page, 'Sources');
  const sourcesFrame = page.getByTestId(testIdForContribution('frisket.core.panel.sources'));
  await expect(sourcesFrame).toBeVisible();
  await expect(sourcesFrame).toHaveAttribute('data-host', 'leftSidebar');
  await expect(sourcesFrame).toHaveAttribute('data-runtime-component-key', 'core.panels.SourcesPanel');

  const leftSidebar = page.getByTestId('workbench-resolved-layout-region-leftSidebar');
  const sourcesItem = leftSidebar.getByTestId(
    'workbench-resolved-layout-item-frisket-core-panel-sources',
  );
  await expect(sourcesItem).toHaveAttribute('data-runtime-source', 'firstParty');
  await expect(sourcesItem).toHaveAttribute('data-slot', 'scope');
});
