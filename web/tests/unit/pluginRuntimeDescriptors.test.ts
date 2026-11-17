import { describe, expect, it } from 'vitest';
import firstPartyPackage from '../../src/assets/first_party_workbench_descriptors.json' with { type: 'json' };
import type {
  WorkbenchPluginDescriptorManifest,
  WorkbenchPluginRuntimeIndex,
  WorkbenchPluginRuntimePlugin,
} from '../../src/api/types';
import {
  firstPartyWorkbenchDescriptorsFromPackage,
  pluginCommandDescriptorsFromRuntimeIndex,
  pluginPanelDescriptorsFromRuntimeIndex,
  pluginViewDescriptorsFromRuntimeIndex,
  type FirstPartyWorkbenchDescriptorPackage,
} from '../../src/workbench/pluginRuntimeDescriptors';

// Parser output for a plugin runtime index that exercises every acceptance and
// rejection rule is pinned by snapshot, including explicit `undefined` fields.
// Keys are sorted: property order is not part of the descriptor contract.

function serialize(value: unknown): string {
  return JSON.stringify(value, (_key, field: unknown) => {
    if (field === undefined) return '<undefined>';
    if (field === null || typeof field !== 'object' || Array.isArray(field)) return field;
    return Object.fromEntries(Object.entries(field).sort(([left], [right]) => left.localeCompare(right)));
  }, 2);
}

const PLUGIN = 'acme.tools';

function manifest(fields: Partial<WorkbenchPluginDescriptorManifest> & Record<string, unknown>): WorkbenchPluginDescriptorManifest {
  return {
    ownerPluginId: PLUGIN,
    title: 'Title',
    placements: [{ host: 'rightInspector', mode: 'panel', order: 2 }],
    ...fields,
  } as WorkbenchPluginDescriptorManifest;
}

const panel = (id: string, extra: Record<string, unknown> = {}) =>
  manifest({ schemaVersion: 'frisket.workbench.panel.v1', kind: 'panel', id, ...extra });
const view = (id: string, extra: Record<string, unknown> = {}) =>
  manifest({ schemaVersion: 'frisket.workbench.view.v1', kind: 'view', id,
    placements: [{ host: 'mainView', mode: 'pane' }], ...extra });
const command = (id: string, extra: Record<string, unknown> = {}) =>
  manifest({ schemaVersion: 'frisket.command.v1', kind: 'command', id, commandId: `${PLUGIN}.${id}`,
    placements: [{ host: 'commandPalette', mode: 'command' }], ...extra });

const manifests: WorkbenchPluginDescriptorManifest[] = [
  panel('acme.tools.panel-b', {
    shortTitle: 'B', icon: 'chart',
    placements: [
      { host: 'rightInspector', mode: 'panel', order: 1, placementId: 'p1', slot: 'detail', default: true,
        density: 'compact', minSize: 10, defaultSize: 20, tabChrome: { closeable: true, singleton: false } },
      { host: 'bottomDock', mode: 'tab' },
    ],
    requires: [{ kind: 'hostCapability', id: 'grid.read', optional: true }, { kind: 'bogus', id: 'x' }, 'junk'],
    dataRequirements: [{ kind: 'activeRow' }, { kind: 'mystery', optional: true }, { kind: 'sheetHasColumnType', columnType: 'date', min: 1 }],
  }),
  panel('acme.tools.panel-a'),
  panel('acme.tools.unbound'),
  panel('acme.tools.no-module'),
  panel('acme.tools.wrong-owner', { ownerPluginId: 'someone.else' }),
  panel('acme.tools.no-title', { title: '  ' }),
  panel('acme.tools.illegal-placement', { placements: [{ host: 'mainView', mode: 'pane' }] }),
  panel('acme.tools.no-placements', { placements: [] }),
  panel('acme.tools.unknown-requirement', { dataRequirements: [{ kind: 'mystery' }] }),
  manifest({ schemaVersion: 'frisket.workbench.panel.v1', kind: 'view', id: 'acme.tools.kind-mismatch' }),
  view('acme.tools.view'),
  view('acme.tools.projection', { projectionKind: 'timeline', projectionParams: { dateColumn: 'd' },
    requires: [{ kind: 'hostCapability', id: 'projection.status' }] }),
  view('acme.tools.projection-undeclared', { projectionKind: 'timeline' }),
  command('acme.tools.command'),
  command('acme.tools.command-unprefixed', { commandId: 'other.command' }),
  command('acme.tools.command-no-id', { commandId: '' }),
  command('acme.tools.no-module-command'),
];

function binding(contributionId: string, moduleUrl: string | undefined) {
  return {
    schemaVersion: 'frisket.workbench_plugin_frontend_component_binding.v1' as const,
    contributionId,
    moduleKey: 'module',
    componentKey: `${contributionId}.Component`,
    moduleUrl,
  };
}

function plugin(overrides: Partial<WorkbenchPluginRuntimePlugin> = {}): WorkbenchPluginRuntimePlugin {
  return {
    schemaVersion: 'frisket.workbench_plugin_runtime_plugin.v1',
    pluginId: PLUGIN,
    version: '1.0.0',
    installState: 'enabled',
    activation: 'manifestLoaded',
    runtimeSource: 'plugin.load_receipt',
    receiptId: null,
    manifestSha256: 'manifest-sha',
    packageSha256: 'package-sha',
    byteCount: 1,
    source: { kind: 'path' },
    contributionSummary: [],
    frontendComponentBindings: manifests
      .map((entry) => String(entry.id))
      .filter((id) => id !== 'acme.tools.unbound')
      .map((id) => binding(id, id.includes('no-module') ? undefined : `/plugins/${id}.js`)),
    workbenchDescriptorManifests: manifests,
    requires: { capabilities: [], secrets: [] },
    arbitraryPackageLoadAllowed: false,
    ...overrides,
  };
}

function runtimeIndex(plugins: WorkbenchPluginRuntimePlugin[], arbitraryPackageLoadAllowed = false): WorkbenchPluginRuntimeIndex {
  return {
    schemaVersion: 'frisket.workbench_plugin_runtime_index.v1',
    projectId: 'project',
    arbitraryPackageLoadAllowed,
    receiptScanLimit: 0,
    skippedInvalidReceipts: 0,
    skippedInvalidManifestRefs: 0,
    loadedPluginCount: plugins.length,
    plugins,
    firstParty: { schemaVersion: 'frisket.workbench_descriptor_package.v1', descriptors: [] },
  };
}

describe('workbench descriptor parsers', () => {
  it('parse every entry of the checked-in first-party package, in order', () => {
    const pkg = firstPartyPackage as FirstPartyWorkbenchDescriptorPackage;
    const descriptors = firstPartyWorkbenchDescriptorsFromPackage(pkg);
    expect(descriptors.map(({ id, kind, schemaVersion }) => ({ id, kind, schemaVersion }))).toEqual(
      pkg.descriptors.map(({ id, kind, schemaVersion }) => ({ id, kind, schemaVersion })),
    );
    for (const descriptor of descriptors) {
      expect(descriptor.runtimeComponent).toBeUndefined();
      expect(descriptor.placements.length).toBeGreaterThan(0);
    }
  });

  it('parse plugin manifests from an enabled plugin and apply every rejection rule', () => {
    const index = runtimeIndex([
      plugin(),
      plugin({ pluginId: 'disabled.plugin', installState: 'disabled' }),
      plugin({ pluginId: 'arbitrary.plugin', arbitraryPackageLoadAllowed: true }),
    ]);
    expect(serialize({
      panels: pluginPanelDescriptorsFromRuntimeIndex(index),
      views: pluginViewDescriptorsFromRuntimeIndex(index),
      commands: pluginCommandDescriptorsFromRuntimeIndex(index),
    })).toMatchSnapshot();
  });

  it('return nothing without an index or when arbitrary package loading is allowed', () => {
    const open = runtimeIndex([plugin()], true);
    for (const collect of [pluginPanelDescriptorsFromRuntimeIndex, pluginViewDescriptorsFromRuntimeIndex, pluginCommandDescriptorsFromRuntimeIndex]) {
      expect(collect(null)).toEqual([]);
      expect(collect(open)).toEqual([]);
    }
  });

  it('fail loudly on a first-party entry that does not parse', () => {
    expect(() => firstPartyWorkbenchDescriptorsFromPackage({
      schemaVersion: 'frisket.workbench_descriptor_package.v1',
      descriptors: [manifest({ schemaVersion: 'frisket.workbench.panel.v1', kind: 'panel', id: 'core.unbound' })],
    })).toThrow('core.unbound');
  });
});
