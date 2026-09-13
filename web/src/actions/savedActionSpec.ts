import {
  isCopilotRegisteredActionDraft,
  isGeneratedActionCatalogEntry,
  type ActionCatalogEntry,
  type ActionCatalogPayload,
  type CopilotRegisteredActionDraft,
  type GeneratedActionCatalogEntry,
} from '../api/types';

export const SAVED_ACTION_SPEC_REFUSAL =
  'This saved action uses an unsupported version or invalid parameters. Recreate the action.';

export class SavedActionSpecError extends Error {
  constructor() {
    super(SAVED_ACTION_SPEC_REFUSAL);
    this.name = 'SavedActionSpecError';
  }
}

export type DecodedSavedActionSpec = {
  entry: GeneratedActionCatalogEntry;
  params: Record<string, unknown>;
  registeredDraft: CopilotRegisteredActionDraft;
};

function plainRecord(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function refuse(): never {
  throw new SavedActionSpecError();
}

function uniqueEntry(
  catalog: ActionCatalogPayload,
  actionKind: string,
): ActionCatalogEntry {
  const matches = catalog.actions.filter((entry) => entry.kind === actionKind);
  if (matches.length !== 1) refuse();
  return matches[0];
}

export function decodeSavedActionSpec(
  catalog: ActionCatalogPayload,
  candidate: unknown,
): DecodedSavedActionSpec {
  if (!plainRecord(candidate)) refuse();
  const draft = 'output_names' in candidate ? candidate : { ...candidate, output_names: {} };
  if (!isCopilotRegisteredActionDraft(draft)) refuse();
  return decodeRegisteredDraft(catalog, draft);
}

function decodeRegisteredDraft(
  catalog: ActionCatalogPayload,
  draft: CopilotRegisteredActionDraft,
): DecodedSavedActionSpec {
  if (Object.keys(draft).some((key) => ![
    'action_id', 'scope', 'params', 'output_names', 'sheet_name',
  ].includes(key))) refuse();
  const entry = uniqueEntry(catalog, draft.action_id);
  if (!isGeneratedActionCatalogEntry(entry)) refuse();
  const createsSheet = entry.ui_hints.typed_action?.creates_sheet === true;
  // Source scope and destination materialization are independent. The host
  // validates whether this producer admits the saved project/row selection.
  if (createsSheet && (typeof draft.sheet_name !== 'string' || !draft.sheet_name.trim())) refuse();
  if (draft.sheet_name !== undefined
    && (typeof draft.sheet_name !== 'string' || !draft.sheet_name.trim())) refuse();
  if (!createsSheet && (draft.scope.kind !== 'sheet_rows'
    || (draft.sheet_name !== undefined && entry.ui_hints.dynamic_outputs !== true))) refuse();
  const expectedOutputs = entry.ui_hints.logical_outputs.map(({ key }) => key);
  const explicitNames = Object.values(draft.output_names);
  const finalNames = expectedOutputs.map((key) => draft.output_names[key] ?? key);
  if (
    Object.entries(draft.output_names).some(([key, name]) => (
      !key || key !== key.trim()
      || typeof name !== 'string' || !name || name !== name.trim()
    ))
    || new Set(explicitNames).size !== explicitNames.length
    || (
      entry.ui_hints.dynamic_outputs !== true
      && (
        Object.keys(draft.output_names).some((key) => !expectedOutputs.includes(key))
        || new Set(finalNames).size !== finalNames.length
      )
    )
  ) refuse();
  return { entry, params: draft.params, registeredDraft: draft };
}

export function encodeSavedActionSpec(
  decoded: DecodedSavedActionSpec,
): Record<string, unknown> {
  return decoded.registeredDraft;
}
