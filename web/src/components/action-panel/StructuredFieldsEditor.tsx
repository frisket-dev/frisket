import { useState } from 'react';

import type { OutputField } from '../../api/open';
import { OutputFieldsEditor } from './OutputFieldsEditor';
import { makeEditableField, toOutputFields, type EditableOutputField } from './outputFieldModel';

/** UI-only ids and partially typed label text stay outside canonical Params. */
export function StructuredFieldsEditor({
  value, onChange, actionKind = 'map.extract', fieldTypes, maxFields, minFields,
}: {
  value: OutputField[];
  onChange(value: OutputField[]): void;
  actionKind?: string;
  fieldTypes?: readonly string[];
  maxFields?: number;
  minFields?: number;
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
      if (field.type === 'list' && previous?.items
        && field.itemMode === previous.itemMode && field.itemFields === previous.itemFields) {
        output[index].items = previous.items;
      }
    }
    setState({ signature: JSON.stringify(output), fields: next });
    onChange(output);
  };
  return <OutputFieldsEditor fields={fields} labelsRaw={labelsRaw}
    actionKind={actionKind} fieldTypes={fieldTypes} maxFields={maxFields} minFields={minFields}
    usesFieldNameDestination={false} outputFieldsReadOnly={false}
    onFieldsChange={update} onLabelsRawChange={setLabelsRaw} />;
}
