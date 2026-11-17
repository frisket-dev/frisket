import type {
  ActionCatalogEntry,
  ActionParam,
  ActionTemplate,
} from '../api/types';

export type CanonicalActionParamValue =
  | null
  | string
  | number
  | boolean
  | CanonicalActionParamValue[]
  | { [name: string]: CanonicalActionParamValue };

export type CanonicalActionDraft = Partial<Record<string, CanonicalActionParamValue>>;
function own(value: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function canonicalActionFieldValue(
  param: ActionParam,
  value: CanonicalActionParamValue,
): CanonicalActionParamValue {
  if (param.input === 'columns') {
    if (!Array.isArray(value) || !value.every((item) => typeof item === 'string')) {
      throw new Error(`Canonical columns field ${param.name} must be a string array`);
    }
    return [...value];
  }
  if (param.input !== 'checkbox') return value;
  if (typeof value === 'boolean') return value;
  if (value !== 'true' && value !== 'false') {
    throw new Error(`Canonical boolean field ${param.name} must be true or false`);
  }
  return value === 'true';
}

function cloneCanonicalValue(value: unknown, fieldName: string): CanonicalActionParamValue {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (Array.isArray(value)) return value.map((item) => cloneCanonicalValue(item, fieldName));
  if (isRecord(value)) {
    return Object.fromEntries(Object.entries(value).map(([name, item]) => [
      name,
      cloneCanonicalValue(item, `${fieldName}.${name}`),
    ]));
  }
  throw new Error(`Canonical action field ${fieldName} is not JSON data`);
}

export function buildCanonicalActionDraft(
  template: ActionTemplate,
  edits: CanonicalActionDraft = {},
): CanonicalActionDraft {
  return Object.fromEntries((template.params ?? []).flatMap((param) => {
    const value = edits[param.name];
    return own(edits, param.name) && value !== undefined
      ? [[param.name, canonicalActionFieldValue(param, value)]]
      : [];
  }));
}

export function canonicalActionDraftFromValidatedParams(
  entry: ActionCatalogEntry,
  params: Readonly<Record<string, unknown>>,
): CanonicalActionDraft {
  if (entry.input_schema.type !== 'object' || !isRecord(params)) {
    throw new Error(`Canonical action params for ${entry.kind} are not an object`);
  }
  const draft = Object.fromEntries(Object.entries(params).map(([name, value]) => [
    name,
    cloneCanonicalValue(value, name),
  ]));
  return draft;
}

export function setCanonicalActionDraftField(
  template: ActionTemplate,
  draft: CanonicalActionDraft,
  name: string,
  value: CanonicalActionParamValue,
): CanonicalActionDraft {
  const param = template.params?.find((candidate) => candidate.name === name);
  if (!param) throw new Error(`Unknown canonical action field: ${name}`);
  return { ...draft, [name]: canonicalActionFieldValue(param, value) };
}

export function serializeCanonicalActionDraft(
  draft: CanonicalActionDraft,
): Record<string, CanonicalActionParamValue> {
  return Object.fromEntries(Object.entries(draft).map(([name, value]) => [
    name,
    cloneCanonicalValue(value, name),
  ]));
}

