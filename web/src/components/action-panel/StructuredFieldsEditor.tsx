import '../PanelPrimitives.css';
import { useState } from 'react';

import type { OutputField, OutputJsonSchema } from '../../api/open';
import { OutputFieldsEditor } from './OutputFieldsEditor';
import { makeEditableField, toOutputFields, type EditableOutputField } from './outputFieldModel';

function patchLockedItemDescriptions(
  items: OutputJsonSchema,
  itemFields: EditableOutputField['itemFields'],
): OutputJsonSchema {
  if (items.type !== 'object' || !items.properties) return items;
  const descriptions = new Map(itemFields.map((field) => [field.name, field.description]));
  return {
    ...items,
    properties: Object.fromEntries(Object.entries(items.properties).map(([name, schema]) => {
      const description = descriptions.get(name);
      if (description === undefined) return [name, schema];
      const trimmedDescription = description.trim();
      if (trimmedDescription) return [name, { ...schema, description: trimmedDescription }];
      return [name, Object.fromEntries(Object.entries(schema)
        .filter(([key]) => key !== 'description'))];
    })) as Record<string, OutputJsonSchema>,
  };
}

/** UI-only ids and partially typed label text stay outside canonical Params. */
export function StructuredFieldsEditor({
  value, onChange, actionKind = 'map.extract', fieldTypes, maxFields, minFields, error,
  labelError, onLabelInteraction, structureLocked = false,
  lockReason = 'Fixed for this comparison.',
}: {
  value: OutputField[];
  onChange(value: OutputField[]): void;
  actionKind?: string;
  fieldTypes?: readonly string[];
  maxFields?: number;
  minFields?: number;
  /** Nested server feedback belongs to this editor, rather than the footer. */
  error?: string;
  labelError?: { fieldIndex: number; message: string };
  onLabelInteraction?(fieldIndex: number): void;
  structureLocked?: boolean;
  lockReason?: string;
}) {
  const [state, setState] = useState(() => ({
    signature: JSON.stringify(value), fields: value.map(makeEditableField),
  }));
  const [labelsRaw, setLabelsRaw] = useState<Record<string, string>>({});
  const signature = JSON.stringify(value);
  let fields = state.fields;
  if (signature !== state.signature) {
    fields = value.map((field, index) => {
      const next = makeEditableField(field);
      const previous = state.fields[index];
      return previous ? { ...next, uiId: previous.uiId,
        itemFields: next.itemFields.map((item, itemIndex) => ({ ...item,
          uiId: previous.itemFields[itemIndex]?.uiId ?? item.uiId })) } : next;
    });
    setState({ signature, fields });
  }
  const update = (mutate: (previous: EditableOutputField[]) => EditableOutputField[]) => {
    const next = mutate(fields);
    const output = toOutputFields(next);
    // Editing a name or label must preserve schema details that the compact
    // list editor does not expose, such as enums or deeply nested properties.
    for (const [index, field] of next.entries()) {
      const previous = fields.find((candidate) => candidate.uiId === field.uiId);
      if (field.type !== 'list' || !previous?.items || field.itemMode !== previous.itemMode) continue;
      if (structureLocked && field.itemMode === 'object') {
        output[index].items = patchLockedItemDescriptions(previous.items, field.itemFields);
      } else if (field.itemFields === previous.itemFields) {
        output[index].items = previous.items;
      }
    }
    setState({ signature: JSON.stringify(output), fields: next });
    onChange(output);
  };
  return <>
    <OutputFieldsEditor fields={fields} labelsRaw={labelsRaw}
      actionKind={actionKind} fieldTypes={fieldTypes} maxFields={maxFields} minFields={minFields}
      usesFieldNameDestination={false} outputFieldsReadOnly={false}
      structureLocked={structureLocked} lockReason={lockReason}
      onFieldsChange={update} onLabelsRawChange={setLabelsRaw}
      labelError={labelError} onLabelInteraction={onLabelInteraction} />
    {error && <p className="panel-error" role="alert">{error}</p>}
  </>;
}
