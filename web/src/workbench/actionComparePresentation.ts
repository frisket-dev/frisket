import type { ActionCatalogEntry, GeneratedActionDraft, JsonSchemaLike, JsonValue } from '../api/types';
import type { HttpSelectorChoicesQuery } from '../api/selectorChoices';
import type { ActionCompareSettingChange } from './ActionCompareTab';

const MAX_VALUE_LENGTH = 96;

type SelectorAuthoredSelection =
  | { kind: 'engine'; engine: string }
  | { kind: 'model'; model: string }
  | { kind: 'engine_model'; engine: string; model: string | null }
  | { kind: 'embedding'; provider: string; model: string };

export interface ActionCompareSelectorChoice {
  label: string;
  authored_selection: SelectorAuthoredSelection;
}

export interface ActionComparePresentationOptions {
  baselineMode: 'preview' | 'current_values';
  lockedParams: readonly string[];
  /** Choices already loaded by the ordinary selector, grouped by semantic field. */
  selectorChoices?: Readonly<Record<string, readonly ActionCompareSelectorChoice[]>>;
  maxChanges?: number;
}

export interface ActionComparePresentation {
  labelForDraft(draft: GeneratedActionDraft, displayKey: string): string;
  describeVariantChanges(
    baseline: GeneratedActionDraft,
    variant: GeneratedActionDraft,
  ): readonly ActionCompareSettingChange[];
}

const isRecord = (value: unknown): value is Record<string, JsonValue> => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
);

function titleize(value: string): string {
  return value.replace(/_/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function labelFor(entry: ActionCatalogEntry, name: string, schema?: JsonSchemaLike): string {
  const declared = entry.ui_hints.form_params?.find((param) => param.name === name)?.label;
  return declared?.trim() || (typeof schema?.title === 'string' && schema.title.trim()) || titleize(name);
}

function bounded(value: string): string {
  const text = value.replace(/\s+/g, ' ').trim();
  if (!text) return 'Empty';
  return text.length <= MAX_VALUE_LENGTH ? text : `${text.slice(0, MAX_VALUE_LENGTH - 1).trimEnd()}…`;
}

function semanticField(entry: ActionCatalogEntry, kind: 'engine' | 'model'): string | undefined {
  const controls = entry.ui_hints.semantic_controls as Record<string, unknown> | undefined;
  return Object.keys(controls ?? {}).find((name) => controls?.[name] === kind);
}

export function actionCompareSelectorField(entry: ActionCatalogEntry): string | null {
  return semanticField(entry, 'engine') ?? semanticField(entry, 'model') ?? null;
}

export function actionCompareSelectorQuery(
  entry: ActionCatalogEntry,
  draft: GeneratedActionDraft,
): HttpSelectorChoicesQuery | null {
  const field = actionCompareSelectorField(entry);
  return field ? {
    schema_version: 'frisket.selector_choices_query.v1',
    subject: { kind: 'action', action_id: entry.kind, field, params: draft.params },
  } as HttpSelectorChoicesQuery : null;
}

function choiceMatches(
  entry: ActionCatalogEntry,
  draft: GeneratedActionDraft,
  choice: ActionCompareSelectorChoice,
): boolean {
  const selection = choice.authored_selection;
  const engine = semanticField(entry, 'engine');
  const model = semanticField(entry, 'model');
  if (selection.kind === 'engine') return Boolean(engine) && draft.params[engine!] === selection.engine;
  if (selection.kind === 'model') return Boolean(model) && draft.params[model!] === selection.model;
  if (selection.kind === 'engine_model') {
    return Boolean(engine) && Boolean(model)
      && draft.params[engine!] === selection.engine && draft.params[model!] === selection.model;
  }
  return Boolean(model) && draft.params[model!] === selection.model;
}

function selectorLabel(
  entry: ActionCatalogEntry,
  draft: GeneratedActionDraft,
  choices: ActionComparePresentationOptions['selectorChoices'],
): string | null {
  for (const fieldChoices of Object.values(choices ?? {})) {
    const match = fieldChoices.find((choice) => choice.label.trim() && choiceMatches(entry, draft, choice));
    if (match) return bounded(match.label);
  }
  return null;
}

function displayValue(
  entry: ActionCatalogEntry,
  name: string,
  value: JsonValue | undefined,
  draft: GeneratedActionDraft,
  choices: ActionComparePresentationOptions['selectorChoices'],
): string | null {
  if (value === undefined || value === null) return 'Not set';
  if (typeof value === 'boolean') return value ? 'On' : 'Off';
  if (typeof value === 'number') return String(value);
  if (typeof value === 'string') {
    const declared = entry.ui_hints.form_params?.find((param) => param.name === name)
      ?.choice_labels?.[value];
    if (declared) return bounded(declared);
    const semantic = (entry.ui_hints.semantic_controls as Record<string, unknown> | undefined)?.[name];
    return semantic === 'engine' || semantic === 'model'
      ? selectorLabel(entry, draft, choices)
      : bounded(value);
  }
  if (Array.isArray(value) && value.every((item) => (
    item === null || ['string', 'number', 'boolean'].includes(typeof item)
  ))) {
    return value.length > 4 ? `${value.length} items` : bounded(value.map(String).join(', '));
  }
  return null;
}

function descriptionChanges(
  before: JsonValue | undefined,
  after: JsonValue | undefined,
): ActionCompareSettingChange[] {
  if (!Array.isArray(before) || !Array.isArray(after) || before.length !== after.length) return [];
  const changes: ActionCompareSettingChange[] = [];
  for (let index = 0; index < before.length; index += 1) {
    const left = before[index];
    const right = after[index];
    if (!isRecord(left) || !isRecord(right) || typeof left.name !== 'string' || left.name !== right.name) return [];
    if (typeof left.description === 'string' && typeof right.description === 'string'
      && left.description !== right.description) {
      changes.push({
        label: `${titleize(left.name)} · Description`,
        before: bounded(left.description),
        after: bounded(right.description),
      });
    }
    const leftLabelDescriptions = left.label_descriptions ?? left.labelDescriptions;
    const rightLabelDescriptions = right.label_descriptions ?? right.labelDescriptions;
    if (JSON.stringify(leftLabelDescriptions) !== JSON.stringify(rightLabelDescriptions)) {
      changes.push({
        label: `${titleize(left.name)} · Label descriptions`,
        before: 'Previous descriptions',
        after: 'Updated descriptions',
      });
    }
  }
  return changes;
}

export function createActionComparePresentation(
  entry: ActionCatalogEntry,
  options: ActionComparePresentationOptions,
): ActionComparePresentation {
  const controls = entry.ui_hints.semantic_controls as Record<string, unknown> | undefined;
  const locked = new Set(options.lockedParams);
  const maxChanges = Math.max(1, Math.min(12, options.maxChanges ?? 8));
  return {
    labelForDraft(draft, displayKey) {
      if (displayKey === 'A') {
        return options.baselineMode === 'current_values' ? 'Current values' : 'Original settings';
      }
      return selectorLabel(entry, draft, options.selectorChoices) ?? `Variant ${displayKey}`;
    },
    describeVariantChanges(baseline, variant) {
      if (baseline.action_id !== entry.kind || variant.action_id !== entry.kind) return [];
      const changes: ActionCompareSettingChange[] = [];
      for (const [name, schema] of Object.entries(entry.input_schema.properties ?? {})) {
        const semantic = controls?.[name];
        if (locked.has(name) || schema.writeOnly === true || schema.format === 'password'
          || ['column', 'columns', 'template', 'rich_source', 'transcript_selection', 'column_or_template'].includes(String(semantic))) {
          continue;
        }
        const before = baseline.params[name];
        const after = variant.params[name];
        if (JSON.stringify(before) === JSON.stringify(after)) continue;
        const beforeLabel = displayValue(entry, name, before, baseline, options.selectorChoices);
        const afterLabel = displayValue(entry, name, after, variant, options.selectorChoices);
        if (beforeLabel !== null && afterLabel !== null) {
          changes.push({ label: labelFor(entry, name, schema), before: beforeLabel, after: afterLabel });
        } else {
          changes.push(...descriptionChanges(before, after));
        }
        if (changes.length >= maxChanges) break;
      }
      return changes.slice(0, maxChanges);
    },
  };
}
