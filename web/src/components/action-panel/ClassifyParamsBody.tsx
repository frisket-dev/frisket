import type { GeneratedActionParams } from '../../generated/actionTypes';
import type { GeneratedActionParamsBodyProps } from './GeneratedActionParamsBody';
import { StructuredFieldsEditor } from './StructuredFieldsEditor';

type ClassifyField = GeneratedActionParams['map.classify']['fields'][number];
const FIELD_TYPES: readonly NonNullable<ClassifyField['type']>[] = ['category', 'score', 'integer', 'number', 'boolean', 'text'];

export function ClassifyParamsBody({ params, setParams, errors, Field }:
  GeneratedActionParamsBodyProps<'map.classify'>) {
  const local = (params.engine ?? 'local_semantic') === 'local_semantic';
  return <>
    <Field name="source" />
    <Field name="engine" />
    <StructuredFieldsEditor actionKind="map.classify" maxFields={local ? 1 : 64}
      fieldTypes={local ? ['category'] : FIELD_TYPES}
      value={(params.fields ?? []).map((field) => ({
        name: field.name, type: field.type ?? 'category', description: field.description ?? '',
        ...(field.labels?.length ? { labels: field.labels } : {}),
        ...(field.label_descriptions && Object.keys(field.label_descriptions).length
          ? { labelDescriptions: field.label_descriptions } : {}),
      }))}
      onChange={(fields) => setParams({ ...params, fields: fields.map((field) => {
        if (!FIELD_TYPES.includes(field.type as NonNullable<ClassifyField['type']>)) throw new Error('Unknown classification field type');
        return {
          name: field.name, type: field.type as ClassifyField['type'], description: field.description,
          ...(field.type === 'category' ? { labels: field.labels ?? [],
            label_descriptions: field.labelDescriptions ?? {} } : {}),
        };
      }) })} />
    {errors.fields?.message && <p className="form-error" role="alert">{errors.fields.message}</p>}
    <Field name="context" />
    {!local && <>
      <Field name="include_confidence" />
      <Field name="include_justification" />
    </>}
  </>;
}
