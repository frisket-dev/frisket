// The PURE editable-output-field model: the EditableOutputField/EditableItemField
// types, schema conversion and initialization helpers, and serialization back to
// public OutputField values. This module sits below both state and rendering —
// actionFormState.ts and OutputFieldsEditor.tsx import it, never the other way
// around — and has no React dependency.
import type { ColumnType, OutputField, OutputJsonSchema } from '../../api/open';

/** A list field's item shape: 'object' (typed sub-fields) or a scalar column
 *  type ('text' | 'number' | 'boolean' | 'date') for a plain list of one type. */
export type ItemMode = 'object' | ColumnType;

export interface EditableOutputField extends OutputField {
  uiId: string;
  itemMode: ItemMode;
  itemFields: EditableItemField[];
}

export interface EditableItemField {
  uiId: string;
  name: string;
  type: ColumnType;
  description: string;
}

let nextFieldUiId = 0;
let nextItemFieldUiId = 0;

/** Mints the React-key uiId for a new nested item field. The counter lives
 *  here (not in the editor) so schema-derived rows (objectItemFields) and
 *  editor-added rows share one id space. */
export function newItemFieldUiId(): string {
  return `item-field-${nextItemFieldUiId++}`;
}

/** The QA "Every row" handoff carries the typed question through
 *  ActionLaunch.initial.prompt (the generalized prefill seam), but extract's
 *  REAL per-row instruction lives in an output
 *  field's description (the "Columns it creates" builder) — `prompt` there
 *  is only "Additional prompt instructions", optional extra guidance
 *  (TEMPLATE_DRIVEN_ACTION_FORMS.extract.promptLabel). Seed exactly ONE
 *  field so the redirect creates exactly one column ("one answer per row"),
 *  instead of extract's unrelated 3-field default (officials/amount_usd/
 *  agency). Name = a short slug from the question's first few words (so the
 *  created column reads as what was asked, not a generic "field_1");
 *  description = the question verbatim, since that field is what actually
 *  drives the model's per-row answer. */
export function extractFieldFromQuestion(question: string): OutputField {
  const words = question
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, ' ')
    .trim()
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 5);
  const name = (words.length ? words.join('_') : 'answer').slice(0, 60);
  return { name, type: 'text', description: question };
}

function schemaTypeForColumnType(type: ColumnType): OutputJsonSchema['type'] {
  if (type === 'number' || type === 'integer' || type === 'boolean') return type;
  if (type === 'json') return 'object';
  if (type === 'list') return 'array';
  return 'string';
}

function columnTypeForSchema(schema: OutputJsonSchema | undefined): ColumnType {
  switch (schema?.type) {
    case 'number':
      return 'number';
    case 'integer':
      return 'integer';
    case 'boolean':
      return 'boolean';
    case 'object':
      return 'json';
    case 'array':
      return 'list';
    default:
      return 'text';
  }
}

function objectItemFields(items: OutputJsonSchema | undefined): EditableItemField[] {
  if (items?.type !== 'object' || !items.properties) return [];
  return Object.entries(items.properties).map(([name, schema]) => ({
    uiId: newItemFieldUiId(),
    name,
    type: columnTypeForSchema(schema),
    description: schema.description ?? '',
  }));
}

function fieldSchema(field: EditableItemField): OutputJsonSchema {
  const schema: OutputJsonSchema = {
    type: schemaTypeForColumnType(field.type),
  };
  const description = field.description.trim();
  if (description) schema.description = description;
  if (schema.type === 'array') schema.items = { type: 'string' };
  return schema;
}

export function toOutputFields(fields: EditableOutputField[]): OutputField[] {
  return fields.map((field) => {
    const out: OutputField = {
      name: field.name,
      type: field.type,
      description: field.description,
      ...(field.labels ? { labels: field.labels } : {}),
      ...(field.labelDescriptions ? { labelDescriptions: field.labelDescriptions } : {}),
      ...(field.required ? { required: true } : {}),
    };
    if (field.type === 'list') {
      if (field.itemMode === 'object' && field.itemFields.length > 0) {
        const properties = field.itemFields.reduce<Record<string, OutputJsonSchema>>(
          (out, item) => {
            const name = item.name.trim();
            if (name) out[name] = fieldSchema(item);
            return out;
          },
          {},
        );
        out.items = {
          type: 'object',
          properties,
          required: Object.keys(properties),
        };
      } else {
        // Plain list of one scalar type (itemMode is a ColumnType here).
        const scalar = field.itemMode === 'object' ? 'text' : field.itemMode;
        out.items = { type: schemaTypeForColumnType(scalar) };
      }
    } else if (field.properties) {
      out.properties = field.properties;
    }
    return out;
  });
}

export function makeEditableField(field: OutputField): EditableOutputField {
  const itemFields = objectItemFields(field.items);
  return {
    ...field,
    uiId: `field-${nextFieldUiId++}`,
    itemMode: field.items?.type === 'object' ? 'object' : columnTypeForSchema(field.items),
    itemFields,
  };
}

/** One field patched by position, the rest untouched — the single shared
 *  "edit field i" transform. OutputFieldsEditor's row callbacks use it, and
 *  so does ActionForm's classify destination combobox (which renames
 *  fields[0] because classify's one category column IS the destination). */
export function patchOutputField(
  fields: EditableOutputField[],
  index: number,
  patch: Partial<OutputField>,
): EditableOutputField[] {
  return fields.map((field, i) => (i === index ? { ...field, ...patch } : field));
}
