// Hand-written types over constants generated from the Python-owned contract
// manifest. CI parity-checks both SDK and frontend mirrors against that manifest.

import {
  PLUGIN_LEGAL_PLACEMENTS as GENERATED_PLUGIN_LEGAL_PLACEMENTS,
  WORKBENCH_SLOT_BY_HOST as GENERATED_WORKBENCH_SLOT_BY_HOST,
  DATA_REQUIREMENT_KINDS as GENERATED_DATA_REQUIREMENT_KINDS,
  CAPABILITY_DEFAULTS as GENERATED_CAPABILITY_DEFAULTS,
  CONTEXT_SCHEMA_VERSIONS as GENERATED_CONTEXT_SCHEMA_VERSIONS,
  DESCRIPTOR_SCHEMA_VERSIONS as GENERATED_DESCRIPTOR_SCHEMA_VERSIONS,
  RESERVED_PLUGIN_IDS as GENERATED_RESERVED_PLUGIN_IDS,
} from './contracts.gen.js';

export type WorkbenchHostId =
  | 'activityRail'
  | 'leftSidebar'
  | 'mainView'
  | 'rightInspector'
  | 'bottomDock'
  | 'modalOrPeek'
  | 'rowDetail'
  | 'entityDetail'
  | 'sourceDetail'
  | 'columnDetail'
  | 'rowInspector'
  | 'columnInspector'
  | 'commandPalette';

export type WorkbenchPlacementMode =
  | 'panel'
  | 'pane'
  | 'tab'
  | 'peek'
  | 'section'
  | 'command';

export type WorkbenchPlacementSlot =
  | 'scope'
  | 'work.primary'
  | 'work.companion'
  | 'inspection'
  | 'configuration'
  | 'companion.output'
  | 'interruption'
  | 'launcher'
  | 'detail';

export type PluginContributionKind = 'panel' | 'view' | 'command';

export interface LegalPlacementPair {
  host: WorkbenchHostId;
  mode: WorkbenchPlacementMode;
}

export const PLUGIN_LEGAL_PLACEMENTS: Record<PluginContributionKind, LegalPlacementPair[]> = {
  panel: [...GENERATED_PLUGIN_LEGAL_PLACEMENTS.panel],
  view: [...GENERATED_PLUGIN_LEGAL_PLACEMENTS.view],
  command: [...GENERATED_PLUGIN_LEGAL_PLACEMENTS.command],
};

export const WORKBENCH_SLOT_BY_HOST: Record<WorkbenchHostId, WorkbenchPlacementSlot> = {
  ...GENERATED_WORKBENCH_SLOT_BY_HOST,
};

export type WorkbenchDataRequirementKind =
  | 'activeProject'
  | 'activeSheet'
  | 'activeRow'
  | 'activeColumn'
  | 'activeCell'
  | 'activeEvidence'
  | 'activeSource'
  | 'activeEntity'
  | 'activeProjection'
  | 'selectedRows'
  | 'sheetHasColumnType';

export const DATA_REQUIREMENT_KINDS: WorkbenchDataRequirementKind[] = [
  ...GENERATED_DATA_REQUIREMENT_KINDS,
];

export const CAPABILITY_DEFAULTS = GENERATED_CAPABILITY_DEFAULTS;

export const CONTEXT_SCHEMA_VERSIONS = GENERATED_CONTEXT_SCHEMA_VERSIONS;

export const DESCRIPTOR_SCHEMA_VERSIONS = GENERATED_DESCRIPTOR_SCHEMA_VERSIONS;

/** Plugin ids reserved by first-party contribution namespaces. */
export const RESERVED_PLUGIN_IDS = GENERATED_RESERVED_PLUGIN_IDS;

// Hand-written mirror of the host regex; regexes are absent from the manifest.
export const PLUGIN_ID_PATTERN =
  /^[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?)*$/;
