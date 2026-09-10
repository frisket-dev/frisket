import type {
  WorkbenchPluginDescriptorManifest,
  WorkbenchPluginRuntimeIndex,
  WorkbenchPluginRuntimePlugin,
} from '../api/types';
import { DATA_REQUIREMENT_CONTEXT_KINDS } from './dataRequirements';
import { firstPartyComponentBinding } from './firstPartyComponents';
import type {
  ColumnInspectorSectionDescriptor,
  RowInspectorSectionDescriptor,
  SourceKindFormDescriptor,
  WorkbenchCommandDescriptor,
  WorkbenchContributionDescriptor,
  WorkbenchDataRequirement,
  WorkbenchHostId,
  WorkbenchPanelDescriptor,
  WorkbenchPlacement,
  WorkbenchPlacementMode,
  WorkbenchPlacementSlot,
  WorkbenchRequirement,
  WorkbenchViewDescriptor,
} from './descriptors';
import { frontendBindingForContribution } from './pluginUiRuntime';

const PANEL_SCHEMA_VERSION = 'frisket.workbench.panel.v1';
const VIEW_SCHEMA_VERSION = 'frisket.workbench.view.v1';
const ROW_INSPECTOR_SECTION_SCHEMA_VERSION = 'frisket.row_inspector.section.v1';
const COLUMN_INSPECTOR_SECTION_SCHEMA_VERSION = 'frisket.column_inspector.section.v1';
const SOURCE_KIND_FORM_SCHEMA_VERSION = 'frisket.source.kind.form.v1';
// The single change point for plugin placement parity: each cohort that opens a
// host/mode pair to plugins adds a row here (parity-tested against the backend
// LEGAL_HOSTS/LEGAL_MODES sets in src/frisket/authoring/workbench/contracts.py).
export const PLUGIN_LEGAL_PLACEMENTS: Record<
  'panel' | 'view' | 'command',
  ReadonlyArray<{ host: WorkbenchPlacement['host']; mode: WorkbenchPlacement['mode'] }>
> = {
  panel: [
    { host: 'rightInspector', mode: 'panel' },
    { host: 'bottomDock', mode: 'tab' },
    { host: 'leftSidebar', mode: 'panel' },
    { host: 'rowDetail', mode: 'tab' },
    { host: 'columnDetail', mode: 'tab' },
    { host: 'columnInspector', mode: 'section' },
    { host: 'entityDetail', mode: 'tab' },
    { host: 'sourceDetail', mode: 'tab' },
    { host: 'modalOrPeek', mode: 'peek' },
    // Launcher-only secondary placement: the rail renders a host-owned
    // reveal/focus button; no plugin code mounts there.
    { host: 'activityRail', mode: 'command' },
  ],
  view: [
    { host: 'mainView', mode: 'pane' },
    { host: 'activityRail', mode: 'command' },
  ],
  command: [{ host: 'commandPalette', mode: 'command' }],
};
// First-party placement legality checks against the backend LEGAL_HOSTS x
// LEGAL_MODES cross product (workbench/contracts.py) instead of the curated
// PLUGIN_LEGAL_PLACEMENTS allowlist above — first-party contributions are
// trusted code, not dispatch-restricted by cohort. PLUGIN_LEGAL_PLACEMENTS
// itself is checked against the generated backend contract manifest by
// test_plugin_sdk_contract_parity.py::test_frontend_tables_match_the_manifest.
const FIRST_PARTY_LEGAL_HOSTS = new Set<WorkbenchHostId>([
  'activityRail',
  'leftSidebar',
  'rightInspector',
  'bottomDock',
  'mainView',
  'modalOrPeek',
  'rowDetail',
  'entityDetail',
  'sourceDetail',
  'columnDetail',
  'rowInspector',
  'columnInspector',
  'commandPalette',
]);
const FIRST_PARTY_LEGAL_MODES = new Set<WorkbenchPlacementMode>([
  'panel',
  'pane',
  'tab',
  'peek',
  'section',
  'command',
]);
const LEGAL_REQUIREMENT_KINDS = new Set<WorkbenchRequirement['kind']>([
  'hostCapability',
  'permission',
  'dataRequirement',
  'contribution',
]);
// One shared kind union (parity-tested against the backend reference); a plugin
// descriptor declaring any known kind reaches the shared enforcement helper.
const LEGAL_DATA_REQUIREMENT_KINDS = new Set<WorkbenchDataRequirement['kind']>([
  ...DATA_REQUIREMENT_CONTEXT_KINDS,
  'selectedRows',
  'sheetHasColumnType',
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function stringValue(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null;
}

function optionalNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function optionalBoolean(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined;
}

// The trust parameter every parser below takes. 'plugin' preserves prior
// behavior exactly (curated placement table, moduleUrl-backed binding,
// lifecycle/enabled gates upstream in the *DescriptorsFromRuntimeIndex
// functions). 'firstParty' differs in EXACTLY three ways: placement
// legality table, in-bundle registry binding instead of moduleUrl, and no
// lifecycle gates (there is no plugin object to gate on). Everything else —
// schema version dispatch, id/title/ownerPluginId presence, requirement-kind
// validation, dataRequirements validation — is one shared code path.
export type WorkbenchDescriptorTrust = 'plugin' | 'firstParty';

interface ResolvedBinding {
  componentKey: string;
  handlerKey?: string;
  runtimeComponent?: NonNullable<WorkbenchPanelDescriptor['runtimeComponent']>;
}

interface ManifestBindingSource {
  trust: WorkbenchDescriptorTrust;
  // 'plugin' trust requires the manifest's ownerPluginId to equal this
  // (namespace fencing); 'firstParty' trust has no owning plugin identity to
  // fence against (first-party ownerPluginId values are core namespaces like
  // frisket.core/frisket.media, not plugin ids) — null skips the check.
  ownerPluginId: string | null;
  // 'plugin' commands must be namespaced under `${plugin.pluginId}.` so they
  // can never shadow core or other plugins' commands; first-party commands
  // have no such prefix requirement — null skips the check.
  commandNamespacePrefix: string | null;
  resolveBinding(contributionId: string): ResolvedBinding | null;
}

function pluginManifestSource(plugin: WorkbenchPluginRuntimePlugin): ManifestBindingSource {
  return {
    trust: 'plugin',
    ownerPluginId: plugin.pluginId,
    commandNamespacePrefix: `${plugin.pluginId}.`,
    resolveBinding(contributionId) {
      const binding = frontendBindingForContribution(plugin, contributionId);
      if (!binding) return null;
      return {
        componentKey: binding.componentKey,
        handlerKey: binding.componentKey,
        runtimeComponent: binding.moduleUrl
          ? {
              pluginId: plugin.pluginId,
              contributionId,
              componentKey: binding.componentKey,
              moduleUrl: binding.moduleUrl,
              moduleKey: binding.moduleKey,
              manifestSha256: plugin.manifestSha256,
              packageSha256: plugin.packageSha256 ?? '',
            }
          : undefined,
      };
    },
  };
}

// Component binding resolves against the in-bundle registry
// (web/src/workbench/firstPartyComponents.ts) instead of requiring
// a moduleUrl — first-party code ships in the app bundle, so there is never
// a runtimeComponent (trust difference: no dynamic-import gate applies).
function firstPartyManifestSource(): ManifestBindingSource {
  return {
    trust: 'firstParty',
    ownerPluginId: null,
    commandNamespacePrefix: null,
    resolveBinding(contributionId) {
      const binding = firstPartyComponentBinding(contributionId);
      if (!binding) return null;
      return { componentKey: binding.componentKey, handlerKey: binding.handlerKey };
    },
  };
}

// Multi-placement policy: every declared placement is validated individually;
// all legal placements are kept. Any placement outside the allowlist rejects
// the whole descriptor (fail closed — the unknown-dataRequirement precedent),
// and zero placements rejects too: a contribution never mounts a partial or
// unenforceable declaration. See FIRST_PARTY_LEGAL_HOSTS/MODES above for the
// trust difference.
function parsePlacements(
  value: unknown,
  trust: WorkbenchDescriptorTrust,
  pluginKind?: keyof typeof PLUGIN_LEGAL_PLACEMENTS,
): WorkbenchPlacement[] | null {
  if (!Array.isArray(value)) return null;
  let legalPairsMap: Map<string, { host: WorkbenchPlacement['host']; mode: WorkbenchPlacement['mode'] }> | null =
    null;
  if (trust === 'plugin') {
    if (!pluginKind) return null;
    legalPairsMap = new Map();
    for (const pair of PLUGIN_LEGAL_PLACEMENTS[pluginKind]) {
      const key = `${pair.host}:${pair.mode}`;
      // First-wins if the table ever holds duplicate pairs.
      if (!legalPairsMap.has(key)) legalPairsMap.set(key, pair);
    }
  }
  const placements: WorkbenchPlacement[] = [];
  for (const item of value) {
    if (!isRecord(item)) return null;
    const rawHost = String(item.host);
    const rawMode = String(item.mode);
    let host: WorkbenchPlacement['host'];
    let mode: WorkbenchPlacement['mode'];
    if (legalPairsMap) {
      const pair = legalPairsMap.get(`${rawHost}:${rawMode}`);
      if (!pair) return null;
      host = pair.host;
      mode = pair.mode;
    } else {
      if (
        !FIRST_PARTY_LEGAL_HOSTS.has(rawHost as WorkbenchHostId) ||
        !FIRST_PARTY_LEGAL_MODES.has(rawMode as WorkbenchPlacementMode)
      ) {
        return null;
      }
      host = rawHost as WorkbenchPlacement['host'];
      mode = rawMode as WorkbenchPlacement['mode'];
    }
    placements.push({
      host,
      mode,
      placementId: stringValue(item.placementId) ?? undefined,
      slot: stringValue(item.slot) as WorkbenchPlacementSlot | undefined,
      default: optionalBoolean(item.default),
      order: optionalNumber(item.order),
      density: stringValue(item.density) as WorkbenchPlacement['density'] | undefined,
      minSize: optionalNumber(item.minSize),
      defaultSize: optionalNumber(item.defaultSize),
      tabChrome: isRecord(item.tabChrome)
        ? {
            closeable: optionalBoolean(item.tabChrome.closeable),
            singleton: optionalBoolean(item.tabChrome.singleton),
          }
        : undefined,
    });
  }
  if (placements.length === 0) return null;
  return placements;
}

function parseRequirements(value: unknown): WorkbenchRequirement[] {
  if (!Array.isArray(value)) return [];
  const requirements: WorkbenchRequirement[] = [];
  for (const item of value) {
    if (!isRecord(item)) continue;
    const kind = stringValue(item.kind) as WorkbenchRequirement['kind'] | null;
    if (!kind || !LEGAL_REQUIREMENT_KINDS.has(kind)) continue;
    requirements.push({
      kind,
      id: stringValue(item.id) ?? undefined,
      optional: optionalBoolean(item.optional),
    });
  }
  return requirements;
}

function parseDataRequirements(value: unknown): WorkbenchDataRequirement[] | null {
  if (!Array.isArray(value)) return [];
  const requirements: WorkbenchDataRequirement[] = [];
  for (const item of value) {
    if (!isRecord(item)) continue;
    const kind = stringValue(item.kind) as WorkbenchDataRequirement['kind'] | null;
    if (!kind) continue;
    if (!LEGAL_DATA_REQUIREMENT_KINDS.has(kind)) {
      // Unknown non-optional requirement: fail closed by rejecting the whole
      // descriptor rather than mounting with an unenforceable declaration.
      if (!optionalBoolean(item.optional)) return null;
      continue;
    }
    requirements.push({
      kind,
      id: stringValue(item.id) ?? undefined,
      columnType: stringValue(item.columnType) ?? undefined,
      min: optionalNumber(item.min),
      optional: optionalBoolean(item.optional),
    });
  }
  return requirements;
}

function runtimePluginIsEnabled(plugin: WorkbenchPluginRuntimePlugin): boolean {
  return plugin.installState === 'enabled';
}

function panelDescriptorFromManifest(
  source: ManifestBindingSource,
  manifest: WorkbenchPluginDescriptorManifest,
): WorkbenchPanelDescriptor | null {
  if (manifest.schemaVersion !== PANEL_SCHEMA_VERSION || manifest.kind !== 'panel') return null;
  const id = stringValue(manifest.id);
  const ownerPluginId = stringValue(manifest.ownerPluginId);
  const title = stringValue(manifest.title);
  if (!id || !ownerPluginId || !title) return null;
  if (source.ownerPluginId !== null && ownerPluginId !== source.ownerPluginId) return null;
  const binding = source.resolveBinding(id);
  if (!binding) return null;
  const placements = parsePlacements(manifest.placements, source.trust, 'panel');
  if (placements === null) return null;
  const dataRequirements = parseDataRequirements(manifest.dataRequirements);
  if (dataRequirements === null) return null;
  return {
    schemaVersion: PANEL_SCHEMA_VERSION,
    id,
    kind: 'panel',
    ownerPluginId,
    title,
    shortTitle: stringValue(manifest.shortTitle) ?? undefined,
    icon: stringValue(manifest.icon) ?? undefined,
    componentKey: binding.componentKey,
    runtimeComponent: binding.runtimeComponent,
    placements,
    requires: parseRequirements(manifest.requires),
    dataRequirements,
  };
}

function viewDescriptorFromManifest(
  source: ManifestBindingSource,
  manifest: WorkbenchPluginDescriptorManifest,
): WorkbenchViewDescriptor | null {
  if (manifest.schemaVersion !== VIEW_SCHEMA_VERSION || manifest.kind !== 'view') return null;
  const id = stringValue(manifest.id);
  const ownerPluginId = stringValue(manifest.ownerPluginId);
  const title = stringValue(manifest.title);
  if (!id || !ownerPluginId || !title) return null;
  if (source.ownerPluginId !== null && ownerPluginId !== source.ownerPluginId) return null;
  const binding = source.resolveBinding(id);
  if (!binding) return null;
  const placements = parsePlacements(manifest.placements, source.trust, 'view');
  if (placements === null) return null;
  const projectionKind = stringValue(manifest.projectionKind) ?? undefined;
  const projectionParams = isRecord(manifest.projectionParams)
    ? { ...manifest.projectionParams }
    : undefined;
  const requires = parseRequirements(manifest.requires);
  // Consistency: projectionKind is the projection-view signal; a projection view
  // must also declare the projection.status host capability it will consume.
  // Fail closed (drop the descriptor) rather than mount a half-declared view.
  if (
    projectionKind &&
    !requires.some(
      (requirement) =>
        requirement.kind === 'hostCapability' && requirement.id === 'projection.status',
    )
  ) {
    return null;
  }
  const dataRequirements = parseDataRequirements(manifest.dataRequirements);
  if (dataRequirements === null) return null;
  return {
    schemaVersion: VIEW_SCHEMA_VERSION,
    id,
    kind: 'view',
    ownerPluginId,
    title,
    shortTitle: stringValue(manifest.shortTitle) ?? undefined,
    icon: stringValue(manifest.icon) ?? undefined,
    componentKey: binding.componentKey,
    runtimeComponent: binding.runtimeComponent,
    placements,
    requires,
    dataRequirements,
    projectionKind,
    projectionParams,
  };
}

const COMMAND_SCHEMA_VERSION = 'frisket.command.v1';

function commandDescriptorFromManifest(
  source: ManifestBindingSource,
  manifest: WorkbenchPluginDescriptorManifest,
): WorkbenchCommandDescriptor | null {
  if (manifest.schemaVersion !== COMMAND_SCHEMA_VERSION || manifest.kind !== 'command') {
    return null;
  }
  const id = stringValue(manifest.id);
  const ownerPluginId = stringValue(manifest.ownerPluginId);
  const title = stringValue(manifest.title);
  if (!id || !ownerPluginId || !title) return null;
  if (source.ownerPluginId !== null && ownerPluginId !== source.ownerPluginId) return null;
  const commandId = stringValue(manifest.commandId);
  // Command ids are namespaced so they can never shadow core (or other
  // plugins') commands; the prefix requirement itself is a plugin-trust-only
  // check (source.commandNamespacePrefix is null for first-party trust).
  if (!commandId) return null;
  if (source.commandNamespacePrefix !== null && !commandId.startsWith(source.commandNamespacePrefix)) {
    return null;
  }
  const binding = source.resolveBinding(id);
  if (!binding) return null;
  // Handlers are functions: plugin trust loads them from the served
  // frontend module (there is no host-component fallback for command
  // handlers); first-party trust resolves them from the in-bundle registry.
  if (source.trust === 'plugin' && !binding.runtimeComponent) return null;
  if (source.trust === 'firstParty' && !binding.handlerKey) return null;
  const placements = parsePlacements(manifest.placements, source.trust, 'command');
  if (placements === null) return null;
  const dataRequirements = parseDataRequirements(manifest.dataRequirements);
  if (dataRequirements === null) return null;
  return {
    schemaVersion: COMMAND_SCHEMA_VERSION,
    id,
    kind: 'command',
    ownerPluginId,
    title,
    shortTitle: stringValue(manifest.shortTitle) ?? undefined,
    icon: stringValue(manifest.icon) ?? undefined,
    componentKey: binding.componentKey,
    commandId,
    handlerKey: binding.handlerKey ?? binding.componentKey,
    runtimeComponent: binding.runtimeComponent,
    placements,
    requires: parseRequirements(manifest.requires),
    dataRequirements,
  };
}

// The two inspectorSection schema versions (row/column) share every field
// except schemaVersion; first-party trust only (no plugin inspectorSection
// contributions exist yet — nothing to unify against).
function inspectorSectionDescriptorFromManifest(
  source: ManifestBindingSource,
  manifest: WorkbenchPluginDescriptorManifest,
  schemaVersion:
    | typeof ROW_INSPECTOR_SECTION_SCHEMA_VERSION
    | typeof COLUMN_INSPECTOR_SECTION_SCHEMA_VERSION,
): RowInspectorSectionDescriptor | ColumnInspectorSectionDescriptor | null {
  if (manifest.schemaVersion !== schemaVersion || manifest.kind !== 'inspectorSection') return null;
  const id = stringValue(manifest.id);
  const ownerPluginId = stringValue(manifest.ownerPluginId);
  const title = stringValue(manifest.title);
  if (!id || !ownerPluginId || !title) return null;
  if (source.ownerPluginId !== null && ownerPluginId !== source.ownerPluginId) return null;
  const binding = source.resolveBinding(id);
  if (!binding) return null;
  const placements = parsePlacements(manifest.placements, source.trust);
  if (placements === null) return null;
  const dataRequirements = parseDataRequirements(manifest.dataRequirements);
  if (dataRequirements === null) return null;
  const base = {
    schemaVersion,
    id,
    kind: 'inspectorSection' as const,
    ownerPluginId,
    title,
    shortTitle: stringValue(manifest.shortTitle) ?? undefined,
    icon: stringValue(manifest.icon) ?? undefined,
    componentKey: binding.componentKey,
    runtimeComponent: binding.runtimeComponent,
    placements,
    requires: parseRequirements(manifest.requires),
    dataRequirements,
  };
  return schemaVersion === ROW_INSPECTOR_SECTION_SCHEMA_VERSION
    ? (base as RowInspectorSectionDescriptor)
    : (base as ColumnInspectorSectionDescriptor);
}

// Source-kind forms are first-party only (no plugin source-provider
// contributions exist yet).
function sourceKindFormDescriptorFromManifest(
  source: ManifestBindingSource,
  manifest: WorkbenchPluginDescriptorManifest,
): SourceKindFormDescriptor | null {
  if (manifest.schemaVersion !== SOURCE_KIND_FORM_SCHEMA_VERSION || manifest.kind !== 'provider') {
    return null;
  }
  const id = stringValue(manifest.id);
  const ownerPluginId = stringValue(manifest.ownerPluginId);
  const title = stringValue(manifest.title);
  if (!id || !ownerPluginId || !title) return null;
  if (source.ownerPluginId !== null && ownerPluginId !== source.ownerPluginId) return null;
  const binding = source.resolveBinding(id);
  if (!binding) return null;
  const placements = parsePlacements(manifest.placements, source.trust);
  if (placements === null) return null;
  const accepts = Array.isArray(manifest.accepts)
    ? manifest.accepts.filter((entry): entry is string => typeof entry === 'string' && entry.trim() !== '')
    : [];
  if (accepts.length === 0) return null;
  const dataRequirements = parseDataRequirements(manifest.dataRequirements);
  if (dataRequirements === null) return null;
  return {
    schemaVersion: SOURCE_KIND_FORM_SCHEMA_VERSION,
    id,
    kind: 'provider',
    ownerPluginId,
    title,
    shortTitle: stringValue(manifest.shortTitle) ?? undefined,
    icon: stringValue(manifest.icon) ?? undefined,
    componentKey: binding.componentKey,
    runtimeComponent: binding.runtimeComponent,
    accepts,
    placements,
    requires: parseRequirements(manifest.requires),
    dataRequirements,
  };
}

export function pluginCommandDescriptorsFromRuntimeIndex(
  runtimeIndex: WorkbenchPluginRuntimeIndex | null,
): WorkbenchCommandDescriptor[] {
  if (!runtimeIndex || runtimeIndex.arbitraryPackageLoadAllowed) return [];
  const descriptors: WorkbenchCommandDescriptor[] = [];
  for (const plugin of runtimeIndex.plugins) {
    if (!runtimePluginIsEnabled(plugin) || plugin.arbitraryPackageLoadAllowed) continue;
    const source = pluginManifestSource(plugin);
    for (const manifest of plugin.workbenchDescriptorManifests ?? []) {
      const descriptor = commandDescriptorFromManifest(source, manifest);
      if (descriptor) descriptors.push(descriptor);
    }
  }
  return descriptors.sort((left, right) => {
    const leftOrder = left.placements[0]?.order ?? 0;
    const rightOrder = right.placements[0]?.order ?? 0;
    return leftOrder - rightOrder || left.id.localeCompare(right.id);
  });
}

export function pluginPanelDescriptorsFromRuntimeIndex(
  runtimeIndex: WorkbenchPluginRuntimeIndex | null,
): WorkbenchPanelDescriptor[] {
  if (!runtimeIndex || runtimeIndex.arbitraryPackageLoadAllowed) return [];
  const descriptors: WorkbenchPanelDescriptor[] = [];
  for (const plugin of runtimeIndex.plugins) {
    if (!runtimePluginIsEnabled(plugin) || plugin.arbitraryPackageLoadAllowed) continue;
    const source = pluginManifestSource(plugin);
    for (const manifest of plugin.workbenchDescriptorManifests ?? []) {
      const descriptor = panelDescriptorFromManifest(source, manifest);
      if (descriptor) descriptors.push(descriptor);
    }
  }
  return descriptors.sort((left, right) => {
    const leftOrder = left.placements[0]?.order ?? 0;
    const rightOrder = right.placements[0]?.order ?? 0;
    return leftOrder - rightOrder || left.id.localeCompare(right.id);
  });
}

export function pluginViewDescriptorsFromRuntimeIndex(
  runtimeIndex: WorkbenchPluginRuntimeIndex | null,
): WorkbenchViewDescriptor[] {
  if (!runtimeIndex || runtimeIndex.arbitraryPackageLoadAllowed) return [];
  const descriptors: WorkbenchViewDescriptor[] = [];
  for (const plugin of runtimeIndex.plugins) {
    if (!runtimePluginIsEnabled(plugin) || plugin.arbitraryPackageLoadAllowed) continue;
    const source = pluginManifestSource(plugin);
    for (const manifest of plugin.workbenchDescriptorManifests ?? []) {
      const descriptor = viewDescriptorFromManifest(source, manifest);
      if (descriptor) descriptors.push(descriptor);
    }
  }
  return descriptors.sort((left, right) => {
    const leftOrder = left.placements[0]?.order ?? 0;
    const rightOrder = right.placements[0]?.order ?? 0;
    return leftOrder - rightOrder || left.id.localeCompare(right.id);
  });
}

// The checked-in artifact (src/frisket/data/first_party_workbench_descriptors.json)
// runs through the SAME parsers as plugin manifests, trust='firstParty'. A
// parse failure here is a build-time invariant violation (our own checked-in
// data failing our own parser), not an untrusted-input case — fail loudly
// instead of silently dropping the contribution.
export interface FirstPartyWorkbenchDescriptorPackage {
  schemaVersion: string;
  descriptors: WorkbenchPluginDescriptorManifest[];
}

export function firstPartyWorkbenchDescriptorsFromPackage(
  pkg: FirstPartyWorkbenchDescriptorPackage,
): WorkbenchContributionDescriptor[] {
  const source = firstPartyManifestSource();
  const descriptors: WorkbenchContributionDescriptor[] = [];
  for (const manifest of pkg.descriptors) {
    const descriptor =
      panelDescriptorFromManifest(source, manifest) ??
      viewDescriptorFromManifest(source, manifest) ??
      commandDescriptorFromManifest(source, manifest) ??
      inspectorSectionDescriptorFromManifest(source, manifest, ROW_INSPECTOR_SECTION_SCHEMA_VERSION) ??
      inspectorSectionDescriptorFromManifest(source, manifest, COLUMN_INSPECTOR_SECTION_SCHEMA_VERSION) ??
      sourceKindFormDescriptorFromManifest(source, manifest);
    if (!descriptor) {
      throw new Error(
        `first-party workbench descriptor package entry failed to parse: ${String(manifest.id)} (${String(manifest.schemaVersion)})`,
      );
    }
    descriptors.push(descriptor);
  }
  return descriptors;
}
