import type { ActionCatalogEntry, GeneratedActionDraft, JsonSchemaLike, JsonValue } from '../api/types';
import type { ActionCompareSettingChange } from './ActionCompareTab';

const DEFAULT_MAX_CHANGES = 8;
const MAX_CHANGE_VALUE_LENGTH = 96;
const MAX_VARIANT_LABEL_LENGTH = 36;

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

function isRecord(value: unknown): value is Record<string, JsonValue> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function own(value: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function titleize(value: string): string {
  return value
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function schemaTitle(schema: JsonSchemaLike | undefined, fallback: string): string {
  const title = schema?.title;
  return typeof title === 'string' && title.trim() ? title.trim() : titleize(fallback);
}

function paramLabel(entry: ActionCatalogEntry, name: string): string {
  const formParam = entry.ui_hints.form_params?.find((param) => param.name === name);
  return formParam?.label?.trim()
    || schemaTitle(entry.input_schema.properties?.[name], name);
}

function isSensitiveName(name: string): boolean {
  return /(^|[_-])(api[_-]?key|authorization|credential|password|secret|token)([_-]|$)/i.test(name);
}

function isSensitiveField(name: string, schema: JsonSchemaLike | undefined): boolean {
  return isSensitiveName(name)
    || schema?.writeOnly === true
    || schema?.format === 'password';
}

function collapseText(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

function boundedText(value: string, limit = MAX_CHANGE_VALUE_LENGTH): string {
  const text = collapseText(value);
  if (!text) return 'Empty';
  return text.length <= limit ? text : `${text.slice(0, limit - 1).trimEnd()}…`;
}

function opaqueIdentifier(value: string): boolean {
  const leaf = value.split(/[/:]/).at(-1) ?? value;
  return leaf.length > 48
    || /^[a-f0-9]{24,}$/i.test(leaf)
    || /^[A-Za-z0-9_-]{40,}$/.test(leaf)
    || /(?:key|secret|token)[_-]?[A-Za-z0-9_-]{12,}/i.test(value);
}

function humanizeSelectorValue(value: string): string {
  if (!value.trim() || opaqueIdentifier(value)) return 'Model choice';
  const withoutDigest = value.split('@')[0] ?? value;
  const leaf = withoutDigest.split(/[/:]/).at(-1) ?? withoutDigest;
  const withoutReleaseDate = leaf.replace(/[-_.]?20\d{6}$/u, '');
  const words = withoutReleaseDate
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .split(/[-_.\s]+/)
    .filter(Boolean)
    .map((word) => {
      const lower = word.toLowerCase();
      if (['ai', 'gpt', 'llm', 'mistral'].includes(lower)) return lower.toUpperCase();
      if (/^\d+b$/i.test(word)) return word.toUpperCase();
      return `${word.charAt(0).toUpperCase()}${word.slice(1)}`;
    })
    .join(' ');
  return boundedText(words || 'Model choice', MAX_VARIANT_LABEL_LENGTH);
}

function selectionMatchesDraft(
  entry: ActionCatalogEntry,
  selectorField: string,
  draft: GeneratedActionDraft,
  selection: SelectorAuthoredSelection,
): boolean {
  const params = draft.params;
  const controls = entry.ui_hints.semantic_controls as Record<string, unknown> | undefined;
  const semanticValue = (kind: 'engine' | 'model'): JsonValue | undefined => {
    if (controls?.[selectorField] === kind) return params[selectorField];
    const field = Object.keys(controls ?? {}).find((name) => controls?.[name] === kind);
    return field ? params[field] : undefined;
  };
  if (selection.kind === 'engine') return semanticValue('engine') === selection.engine;
  if (selection.kind === 'model') return semanticValue('model') === selection.model;
  if (selection.kind === 'engine_model') {
    return semanticValue('engine') === selection.engine
      && semanticValue('model') === selection.model;
  }
  return semanticValue('model') === selection.model;
}

function selectorChoiceLabel(
  entry: ActionCatalogEntry,
  choicesByField: ActionComparePresentationOptions['selectorChoices'],
  draft: GeneratedActionDraft,
): string | null {
  for (const [field, choices] of Object.entries(choicesByField ?? {})) {
    const match = choices.find((choice) => (
      choice.label.trim() && selectionMatchesDraft(entry, field, draft, choice.authored_selection)
    ));
    if (match) return boundedText(match.label, MAX_VARIANT_LABEL_LENGTH);
  }
  return null;
}

function catalogValueLabel(
  entry: ActionCatalogEntry,
  name: string,
  value: JsonValue | undefined,
  draft: GeneratedActionDraft,
  choicesByField: ActionComparePresentationOptions['selectorChoices'],
): string | null {
  if (typeof value !== 'string') return null;
  const formParam = entry.ui_hints.form_params?.find((param) => param.name === name);
  const choiceLabel = formParam?.choice_labels?.[value];
  if (choiceLabel?.trim()) return boundedText(choiceLabel);

  const controls = entry.ui_hints.semantic_controls as Record<string, unknown> | undefined;
  const semantic = controls?.[name];
  if (semantic === 'model' || semantic === 'engine') {
    const selectorLabel = selectorChoiceLabel(entry, choicesByField, draft);
    if (selectorLabel) return selectorLabel;
  }
  if (semantic === 'engine') {
    const engine = entry.ui_hints.engines?.find((candidate) => (
      candidate.id === value || candidate.target_id === value
    ));
    if (engine?.label.trim()) return boundedText(engine.label);
  }
  if (semantic === 'model') return humanizeSelectorValue(value);
  return null;
}

function formatValue(
  entry: ActionCatalogEntry,
  name: string,
  value: JsonValue | undefined,
  draft: GeneratedActionDraft,
  choicesByField: ActionComparePresentationOptions['selectorChoices'],
): string {
  const catalogLabel = catalogValueLabel(entry, name, value, draft, choicesByField);
  if (catalogLabel) return catalogLabel;
  if (value === undefined || value === null) return 'Not set';
  if (typeof value === 'boolean') return value ? 'On' : 'Off';
  if (typeof value === 'number') return String(value);
  if (typeof value === 'string') return boundedText(value);
  if (Array.isArray(value) && value.every((item) => (
    item === null || ['string', 'number', 'boolean'].includes(typeof item)
  ))) {
    if (value.length === 0) return 'None';
    if (value.length > 4) return `${value.length} items`;
    return boundedText(value.map((item) => item === null ? 'Empty' : String(item)).join(', '));
  }
  return 'Updated';
}

function sameValue(left: JsonValue | undefined, right: JsonValue | undefined): boolean {
  if (left === right) return true;
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((value, index) => sameValue(value, right[index]));
  }
  if (isRecord(left) && isRecord(right)) {
    const keys = new Set([...Object.keys(left), ...Object.keys(right)]);
    return [...keys].every((key) => sameValue(left[key], right[key]));
  }
  return false;
}

function stableItemName(left: JsonValue, right: JsonValue): string | null {
  if (!isRecord(left) || !isRecord(right)) return null;
  for (const key of ['name', 'label', 'key', 'id']) {
    const leftValue = left[key];
    if (typeof leftValue === 'string' && leftValue === right[key]
      && leftValue.trim() && !opaqueIdentifier(leftValue)) {
      return titleize(boundedText(leftValue, 40));
    }
  }
  return null;
}

interface ChangeCollectorContext {
  entry: ActionCatalogEntry;
  baseline: GeneratedActionDraft;
  variant: GeneratedActionDraft;
  selectorChoices: ActionComparePresentationOptions['selectorChoices'];
  changes: ActionCompareSettingChange[];
  maxChanges: number;
}

function collectChanges(
  context: ChangeCollectorContext,
  name: string,
  labelParts: readonly string[],
  before: JsonValue | undefined,
  after: JsonValue | undefined,
  schema: JsonSchemaLike | undefined,
): void {
  if (context.changes.length >= context.maxChanges || sameValue(before, after)) return;
  if (isSensitiveField(name, schema)) return;

  if (Array.isArray(before) && Array.isArray(after)) {
    const primitive = [...before, ...after].every((value) => (
      value === null || ['string', 'number', 'boolean'].includes(typeof value)
    ));
    if (primitive) {
      context.changes.push({
        label: labelParts.join(' · '),
        before: formatValue(context.entry, name, before, context.baseline, context.selectorChoices),
        after: formatValue(context.entry, name, after, context.variant, context.selectorChoices),
      });
      return;
    }
    if (before.length !== after.length) return;
    for (let index = 0; index < before.length && context.changes.length < context.maxChanges; index += 1) {
      const itemLabel = stableItemName(before[index]!, after[index]!) ?? `${labelParts.at(-1)} ${index + 1}`;
      collectChanges(context, name, [itemLabel], before[index], after[index], schema?.items);
    }
    return;
  }

  if (isRecord(before) && isRecord(after)) {
    const schemaProperties = Object.keys(schema?.properties ?? {});
    const keys = [...schemaProperties, ...Object.keys(before), ...Object.keys(after)]
      .filter((key, index, all) => all.indexOf(key) === index);
    for (const key of keys) {
      if (context.changes.length >= context.maxChanges) break;
      const childSchema = schema?.properties?.[key];
      collectChanges(
        context,
        key,
        [...labelParts, schemaTitle(childSchema, key)],
        before[key],
        after[key],
        childSchema,
      );
    }
    return;
  }

  context.changes.push({
    label: labelParts.join(' · '),
    before: formatValue(context.entry, name, before, context.baseline, context.selectorChoices),
    after: formatValue(context.entry, name, after, context.variant, context.selectorChoices),
  });
}

/**
 * Adapts the action catalog's existing presentation metadata to the compact
 * labels and safe setting summaries used by Action Compare.
 */
export function createActionComparePresentation(
  entry: ActionCatalogEntry,
  options: ActionComparePresentationOptions,
): ActionComparePresentation {
  const lockedParams = new Set(options.lockedParams);
  const controls = entry.ui_hints.semantic_controls as Record<string, unknown> | undefined;
  const maxChanges = Math.max(1, Math.min(12, options.maxChanges ?? DEFAULT_MAX_CHANGES));
  const selectorNames = Object.keys(controls ?? {}).filter((name) => (
    controls?.[name] === 'model' || controls?.[name] === 'engine'
  ));

  return {
    labelForDraft(draft, displayKey) {
      if (displayKey === 'A') {
        return options.baselineMode === 'current_values' ? 'Current values' : 'Original settings';
      }
      const selectorLabel = selectorChoiceLabel(entry, options.selectorChoices, draft);
      if (selectorLabel) return selectorLabel;
      const modelName = selectorNames.find((name) => controls?.[name] === 'model');
      if (modelName) {
        const label = catalogValueLabel(entry, modelName, draft.params[modelName], draft, options.selectorChoices);
        if (label && label !== 'Model choice') return label;
      }
      const engineName = selectorNames.find((name) => controls?.[name] === 'engine');
      if (engineName) {
        const label = catalogValueLabel(entry, engineName, draft.params[engineName], draft, options.selectorChoices);
        if (label) return label;
      }
      return `Variant ${displayKey}`;
    },

    describeVariantChanges(baseline, variant) {
      if (baseline.action_id !== entry.kind || variant.action_id !== entry.kind) return [];
      const schemaProperties = entry.input_schema.properties ?? {};
      const orderedNames = [...Object.keys(schemaProperties), ...Object.keys(baseline.params), ...Object.keys(variant.params)]
        .filter((name, index, all) => all.indexOf(name) === index);
      const context: ChangeCollectorContext = {
        entry,
        baseline,
        variant,
        selectorChoices: options.selectorChoices,
        changes: [],
        maxChanges,
      };
      for (const name of orderedNames) {
        if (context.changes.length >= maxChanges) break;
        const semantic = controls?.[name];
        if (lockedParams.has(name)
          || ['column', 'columns', 'template', 'rich_source', 'transcript_selection', 'column_or_template'].includes(String(semantic))) {
          continue;
        }
        const schema = schemaProperties[name];
        if (isSensitiveField(name, schema)) continue;
        collectChanges(
          context,
          name,
          [paramLabel(entry, name)],
          own(baseline.params, name) ? baseline.params[name] : undefined,
          own(variant.params, name) ? variant.params[name] : undefined,
          schema,
        );
      }
      return context.changes;
    },
  };
}
