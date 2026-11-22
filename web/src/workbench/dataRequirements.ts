import type { WorkbenchDataRequirement, WorkbenchRequirement } from './descriptors';

// Mirror of DATA_REQUIREMENT_CONTEXT_KINDS in src/frisket/authoring/workbench/contracts.py.
// The parity test in tests/authoring/test_workbench_descriptor_metadata_honesty.py binds the
// two unions; change them together.
export const DATA_REQUIREMENT_CONTEXT_KINDS = [
  'activeProject',
  'activeSheet',
  'activeRow',
  'activeColumn',
  'activeCell',
  'activeEvidence',
  'activeFeed',
  'activeEntity',
  'activeProjection',
] as const;

type DataRequirementContextKind = (typeof DATA_REQUIREMENT_CONTEXT_KINDS)[number];

export interface WorkbenchDataRequirementContext {
  activeProject: boolean;
  activeSheet: boolean;
  activeRow: boolean;
  activeColumn: boolean;
  activeCell: boolean;
  activeEvidence: boolean;
  activeFeed: boolean;
  activeEntity: boolean;
  activeProjection: boolean;
  selectedRowIds: readonly string[];
  columnTypes: readonly string[];
}

const EMPTY_DATA_REQUIREMENT_CONTEXT: WorkbenchDataRequirementContext = {
  activeProject: false,
  activeSheet: false,
  activeRow: false,
  activeColumn: false,
  activeCell: false,
  activeEvidence: false,
  activeFeed: false,
  activeEntity: false,
  activeProjection: false,
  selectedRowIds: [],
  columnTypes: [],
};

interface UnmetDataRequirement {
  kind: WorkbenchDataRequirement['kind'];
  columnType?: string;
}

export function sheetDataRequirementContext(
  sheet: { columns: readonly { type: string }[] } | null,
): WorkbenchDataRequirementContext {
  return {
    ...EMPTY_DATA_REQUIREMENT_CONTEXT,
    activeProject: true,
    activeSheet: sheet !== null,
    columnTypes: sheet ? sheet.columns.map((column) => column.type) : [],
  };
}

function isContextKind(kind: string): kind is DataRequirementContextKind {
  return (DATA_REQUIREMENT_CONTEXT_KINDS as readonly string[]).includes(kind);
}

export function firstMissingDataRequirement(
  requirements: readonly WorkbenchDataRequirement[] | undefined,
  context: WorkbenchDataRequirementContext,
): UnmetDataRequirement | null {
  const columnTypesSet = new Set(context.columnTypes);
  for (const requirement of requirements ?? []) {
    if (requirement.optional) continue;
    if (isContextKind(requirement.kind)) {
      if (!context[requirement.kind]) {
        return { kind: requirement.kind };
      }
      continue;
    }
    if (requirement.kind === 'sheetHasColumnType') {
      if (!requirement.columnType || !columnTypesSet.has(requirement.columnType)) {
        return { kind: requirement.kind, columnType: requirement.columnType };
      }
      continue;
    }
    if (requirement.kind === 'selectedRows') {
      if (context.selectedRowIds.length < (requirement.min ?? 1)) {
        return { kind: requirement.kind };
      }
    }
  }
  return null;
}

export function dataRequirementReason(unmet: UnmetDataRequirement): string {
  if (unmet.kind === 'sheetHasColumnType' && unmet.columnType) {
    return `data_requirement_unmet:sheetHasColumnType:${unmet.columnType}`;
  }
  return `data_requirement_unmet:${unmet.kind}`;
}

export type RequirementAvailability =
  | { available: true; reason?: undefined }
  | { available: false; reason: string };

/** The first missing required host capability, else the first unmet data requirement. */
export function resolveRequirementAvailability(
  descriptor: { requires: readonly WorkbenchRequirement[]; dataRequirements?: readonly WorkbenchDataRequirement[] },
  capabilities: Set<string>,
  context: WorkbenchDataRequirementContext,
): RequirementAvailability {
  for (const requirement of descriptor.requires) {
    if (requirement.kind !== 'hostCapability' || requirement.optional) continue;
    const capabilityId = requirement.id ?? '';
    if (!capabilities.has(capabilityId)) {
      return { available: false, reason: `missing_capability:${capabilityId}` };
    }
  }
  const unmet = firstMissingDataRequirement(descriptor.dataRequirements, context);
  if (unmet) {
    return { available: false, reason: dataRequirementReason(unmet) };
  }
  return { available: true };
}

/** `capabilities` minus the ids a test harness disabled through a window global. */
export function withoutDisabledCapabilities(
  capabilities: readonly string[],
  disabled: (window: Window) => readonly string[] | undefined,
): Set<string> {
  const disabledSet = new Set(typeof window === 'undefined' ? [] : disabled(window) ?? []);
  return new Set(capabilities.filter((capability) => !disabledSet.has(capability)));
}
