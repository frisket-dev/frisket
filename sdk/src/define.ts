import {
  DESCRIPTOR_SCHEMA_VERSIONS,
  PLUGIN_ID_PATTERN,
  PLUGIN_LEGAL_PLACEMENTS,
  RESERVED_PLUGIN_IDS,
  WORKBENCH_SLOT_BY_HOST,
  type LegalPlacementPair,
  type PluginContributionKind,
  type WorkbenchDataRequirementKind,
  type WorkbenchHostId,
  type WorkbenchPlacementMode,
  type WorkbenchPlacementSlot,
} from './contracts.js';

export interface PlacementInput {
  host: WorkbenchHostId;
  mode: WorkbenchPlacementMode;
  slot?: WorkbenchPlacementSlot;
  placementId?: string;
  order?: number;
  default?: boolean;
}

export interface DataRequirement {
  kind: WorkbenchDataRequirementKind;
  columnType?: string;
  min?: number;
  optional?: boolean;
}

export interface HostRequirement {
  kind: 'hostCapability';
  id: string;
  optional?: boolean;
}

/** Declarative data-requirement helpers. */
export const needs = {
  activeProject: (): DataRequirement => ({ kind: 'activeProject' }),
  activeSheet: (): DataRequirement => ({ kind: 'activeSheet' }),
  activeRow: (): DataRequirement => ({ kind: 'activeRow' }),
  activeColumn: (): DataRequirement => ({ kind: 'activeColumn' }),
  activeCell: (): DataRequirement => ({ kind: 'activeCell' }),
  activeEvidence: (): DataRequirement => ({ kind: 'activeEvidence' }),
  activeSource: (): DataRequirement => ({ kind: 'activeSource' }),
  activeEntity: (): DataRequirement => ({ kind: 'activeEntity' }),
  activeProjection: (): DataRequirement => ({ kind: 'activeProjection' }),
  selectedRows: (min?: number): DataRequirement =>
    min === undefined ? { kind: 'selectedRows' } : { kind: 'selectedRows', min },
  sheetHasColumnType: (columnType: string): DataRequirement => ({
    kind: 'sheetHasColumnType',
    columnType,
  }),
};

export function capability(id: string, options?: { optional?: boolean }): HostRequirement {
  if (typeof id !== 'string' || id.length === 0) {
    throw new Error('capability(id): id must be a non-empty string');
  }
  return options?.optional ? { kind: 'hostCapability', id, optional: true } : { kind: 'hostCapability', id };
}

interface ContributionCommon {
  /** Short key; the full contribution id becomes `<pluginId>.<prefix>.<key>`. */
  key: string;
  title: string;
  shortTitle?: string;
  icon?: string;
  /** Export name in the frontend module. */
  component: string;
  placements: PlacementInput[];
  requires?: HostRequirement[];
  needs?: DataRequirement[];
}

export interface PanelDefinition extends ContributionCommon {
  kind: 'panel';
}
export interface ViewDefinition extends ContributionCommon {
  kind: 'view';
  projectionKind?: string;
  projectionParams?: Record<string, unknown>;
}
export interface CommandDefinition extends Omit<ContributionCommon, 'placements'> {
  kind: 'command';
  placements?: PlacementInput[];
}

const KEY_PATTERN = /^[a-z0-9][a-z0-9_]*$/;

function assertKey(kind: string, key: string): void {
  if (!KEY_PATTERN.test(key)) {
    throw new Error(
      `${kind} key "${key}" is invalid: keys are lowercase alphanumeric/underscore (the full id becomes <pluginId>.<kind>.<key>)`,
    );
  }
}

function assertNoFunctions(value: unknown, path: string): void {
  if (typeof value === 'function') {
    throw new Error(
      `${path} is a function — descriptors are declarative data; use needs.* / capability() helpers instead of callbacks`,
    );
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertNoFunctions(item, `${path}[${index}]`));
  } else if (value !== null && typeof value === 'object') {
    for (const [k, v] of Object.entries(value)) assertNoFunctions(v, `${path}.${k}`);
  }
}

function assertLegalPlacements(
  kind: PluginContributionKind,
  id: string,
  placements: PlacementInput[],
): void {
  if (placements.length === 0) {
    throw new Error(`${kind} "${id}" declares no placements`);
  }
  const legal = PLUGIN_LEGAL_PLACEMENTS[kind];
  for (const placement of placements) {
    const ok = legal.some(
      (pair: LegalPlacementPair) => pair.host === placement.host && pair.mode === placement.mode,
    );
    if (!ok) {
      const legalText = legal.map((pair) => `${pair.host}:${pair.mode}`).join(', ');
      throw new Error(
        `${kind} "${id}" placement ${placement.host}:${placement.mode} is not legal for plugin ${kind}s (legal: ${legalText})`,
      );
    }
  }
}

export function definePanel(definition: Omit<PanelDefinition, 'kind'>): PanelDefinition {
  assertKey('panel', definition.key);
  return { kind: 'panel', ...definition };
}

export function defineView(definition: Omit<ViewDefinition, 'kind'>): ViewDefinition {
  assertKey('view', definition.key);
  return { kind: 'view', ...definition };
}

export function defineCommand(definition: Omit<CommandDefinition, 'kind'>): CommandDefinition {
  assertKey('command', definition.key);
  return { kind: 'command', ...definition };
}

/** Trusted handler for one projection kind's status and build planning. */
export interface ProjectionDefinition {
  kind: 'projection';
  /** Short key; the projection kind becomes `<pluginId>.projection.<key>`. */
  key: string;
  title: string;
  /** Python handler function key relative to the plugin (`plugin.py` unless
   * `modulePath` overrides), decorated with `@plugin.projection(...)`. */
  handler: string;
  modulePath?: string;
  /** Host-recognized projection role this binding fills (e.g. 'map_points'). */
  role?: string;
}

export function defineProjection(
  definition: Omit<ProjectionDefinition, 'kind'>,
): ProjectionDefinition {
  assertKey('projection', definition.key);
  return { kind: 'projection', ...definition };
}

export interface PluginDefinition {
  id: string;
  version: string;
  /** Path of the built frontend module inside the package. */
  module?: string;
  views?: ViewDefinition[];
  panels?: PanelDefinition[];
  commands?: CommandDefinition[];
  projections?: ProjectionDefinition[];
  capabilities?: string[];
  secrets?: unknown[];
}

export interface DefinedPlugin {
  manifest: Record<string, unknown>;
  descriptors: Record<string, unknown>;
}

function normalizePlacement(placement: PlacementInput): Record<string, unknown> {
  const slot = placement.slot ?? WORKBENCH_SLOT_BY_HOST[placement.host];
  const out: Record<string, unknown> = {
    host: placement.host,
    mode: placement.mode,
    slot,
  };
  if (placement.placementId !== undefined) out.placementId = placement.placementId;
  if (placement.order !== undefined) out.order = placement.order;
  if (placement.default !== undefined) out.default = placement.default;
  return out;
}

export function definePlugin(definition: PluginDefinition): DefinedPlugin {
  const { id: pluginId, version } = definition;
  if (!PLUGIN_ID_PATTERN.test(pluginId)) {
    throw new Error(`plugin id "${pluginId}" does not match the plugin id grammar`);
  }
  if ((RESERVED_PLUGIN_IDS as readonly string[]).includes(pluginId)) {
    throw new Error(
      `plugin id "${pluginId}" is reserved (first-party contribution namespace); pick your own namespace`,
    );
  }
  const modulePath = definition.module ?? 'frontend/plugin.js';
  const moduleKey = `${pluginId}.ui`;

  const views = definition.views ?? [];
  const panels = definition.panels ?? [];
  const commands = definition.commands ?? [];
  const projections = definition.projections ?? [];

  const descriptors: Record<string, unknown>[] = [];
  const components: Record<string, unknown>[] = [];

  const seenIds = new Set<string>();
  const claimId = (contributionId: string): string => {
    if (seenIds.has(contributionId)) {
      throw new Error(`duplicate contribution id "${contributionId}"`);
    }
    seenIds.add(contributionId);
    return contributionId;
  };

  const pushComponent = (contributionId: string, componentKind: 'components' | 'commands', exportName: string) => {
    components.push({
      contribution_id: contributionId,
      module_key: moduleKey,
      component_key: `${pluginId}.${componentKind}.${exportName}`,
      module_path: modulePath,
    });
  };

  for (const view of views) {
    const contributionId = claimId(`${pluginId}.view.${view.key}`);
    assertLegalPlacements('view', contributionId, view.placements);
    if (
      view.projectionKind &&
      !(view.requires ?? []).some(
        (requirement) => requirement.kind === 'hostCapability' && requirement.id === 'projection.status',
      )
    ) {
      throw new Error(
        `view "${contributionId}" declares projectionKind but not capability('projection.status') — the host drops projection views without it`,
      );
    }
    const descriptor: Record<string, unknown> = {
      schemaVersion: DESCRIPTOR_SCHEMA_VERSIONS.view,
      id: contributionId,
      kind: 'view',
      title: view.title,
      ...(view.shortTitle !== undefined ? { shortTitle: view.shortTitle } : {}),
      ...(view.icon !== undefined ? { icon: view.icon } : {}),
      ownerPluginId: pluginId,
      componentKey: `${pluginId}.components.${view.component}`,
      placements: view.placements.map(normalizePlacement),
      requires: view.requires ?? [],
      dataRequirements: view.needs ?? [],
      ...(view.projectionKind !== undefined ? { projectionKind: view.projectionKind } : {}),
      ...(view.projectionParams !== undefined ? { projectionParams: view.projectionParams } : {}),
    };
    descriptors.push(descriptor);
    pushComponent(contributionId, 'components', view.component);
  }

  for (const panel of panels) {
    const contributionId = claimId(`${pluginId}.panel.${panel.key}`);
    assertLegalPlacements('panel', contributionId, panel.placements);
    descriptors.push({
      schemaVersion: DESCRIPTOR_SCHEMA_VERSIONS.panel,
      id: contributionId,
      kind: 'panel',
      title: panel.title,
      ...(panel.shortTitle !== undefined ? { shortTitle: panel.shortTitle } : {}),
      ...(panel.icon !== undefined ? { icon: panel.icon } : {}),
      ownerPluginId: pluginId,
      componentKey: `${pluginId}.components.${panel.component}`,
      placements: panel.placements.map(normalizePlacement),
      requires: panel.requires ?? [],
      dataRequirements: panel.needs ?? [],
    });
    pushComponent(contributionId, 'components', panel.component);
  }

  for (const command of commands) {
    const contributionId = claimId(`${pluginId}.command.${command.key}`);
    const placements = command.placements ?? [
      { host: 'commandPalette', mode: 'command' } as PlacementInput,
    ];
    assertLegalPlacements('command', contributionId, placements);
    descriptors.push({
      schemaVersion: DESCRIPTOR_SCHEMA_VERSIONS.command,
      id: contributionId,
      kind: 'command',
      title: command.title,
      ...(command.shortTitle !== undefined ? { shortTitle: command.shortTitle } : {}),
      ...(command.icon !== undefined ? { icon: command.icon } : {}),
      ownerPluginId: pluginId,
      componentKey: `${pluginId}.commands.${command.component}`,
      commandId: contributionId,
      handlerKey: `${pluginId}.commands.${command.component}`,
      placements: placements.map(normalizePlacement),
      requires: command.requires ?? [],
      dataRequirements: command.needs ?? [],
    });
    pushComponent(contributionId, 'commands', command.component);
  }

  const runtimeProjections = projections.map((projection) => {
    const projectionKind = claimId(`${pluginId}.projection.${projection.key}`);
    return {
      kind: projectionKind,
      handler_key: `${pluginId}:${projection.handler}`,
      handler_api: 'plugin_projection',
      title: projection.title,
      module_path: projection.modulePath ?? 'plugin.py',
      // Planning only; projection data stays host-owned.
      execution: {
        mode: 'runtime_plan',
        ...(projection.role !== undefined ? { role: projection.role } : {}),
      },
    };
  });

  const manifest = {
    schema_version: DESCRIPTOR_SCHEMA_VERSIONS.plugin,
    id: pluginId,
    version,
    contributes: {
      workbench_views: views.map((view) => `${pluginId}.view.${view.key}`),
      workbench_panels: panels.map((panel) => `${pluginId}.panel.${panel.key}`),
      workbench_commands: commands.map((command) => `${pluginId}.command.${command.key}`),
      // Actions are declared natively in Python (`Plugin(actions=(...))`), which
      // generates their manifest entries; this SDK authors workbench and
      // projection contributions only, so it always emits the key empty.
      actions: [],
      importers: [],
      operators: [],
      projections: runtimeProjections.map((projection) => projection.kind),
      column_types: [],
      job_handlers: [],
    },
    requires: {
      capabilities: definition.capabilities ?? [],
      secrets: definition.secrets ?? [],
    },
    runtime: {
      actions: [],
      importers: [],
      operators: [],
      projections: runtimeProjections,
      job_handlers: [],
      workbench_components: components,
    },
  };

  const descriptorPackage = {
    schemaVersion: DESCRIPTOR_SCHEMA_VERSIONS.descriptorPackage,
    descriptors,
  };

  assertNoFunctions(manifest, 'manifest');
  assertNoFunctions(descriptorPackage, 'descriptors');

  return { manifest, descriptors: descriptorPackage };
}
