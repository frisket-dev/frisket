import type { JsonValue, RegisteredActionDraft, DeriveCompositeRequest } from '../api/types';
import type { V1ActionSpecActionOutput } from '../api/v1ActionSession';

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function namedResultForListTable(
  outputs: V1ActionSpecActionOutput[] | undefined,
  itemField: string,
): V1ActionSpecActionOutput {
  const output = (outputs ?? []).find((candidate) => {
    const ref = candidate.ref ?? {};
    return candidate.kind === 'named_result'
      && (!Array.isArray(ref.may_feed) || ref.may_feed.includes('derive.table_from_list'))
      && (candidate.name === itemField || ref.route === itemField);
  });
  if (!output?.ref) {
    throw new Error(`map.extract did not return a derive-ready named result for ${itemField}`);
  }
  return output;
}

function columnType(schema: Record<string, unknown>): string {
  if (['integer', 'number', 'boolean'].includes(String(schema.type))) return String(schema.type);
  return schema.type === 'array' || schema.type === 'object' ? 'json' : 'text';
}

export function listTableFromNamedResult(
  req: DeriveCompositeRequest,
  itemField: string,
  output: V1ActionSpecActionOutput,
): RegisteredActionDraft {
  const ref = output.ref ?? {};
  const sheetId = ref.sheet_id ?? output.sheet_id;
  const columnId = ref.column_id ?? output.column_id;
  const route = ref.route ?? output.name;
  const schema = ref.schema ?? `${route}_list`;
  if (typeof sheetId !== 'number' || typeof columnId !== 'number'
    || typeof ref.run_id !== 'number' || typeof route !== 'string' || !route.trim()
    || typeof schema !== 'string' || !schema.trim()) {
    throw new Error('map.extract returned an invalid named result ref');
  }
  const fields = req.extraction.params.fields;
  const declared = Array.isArray(fields)
    ? fields.find((field) => isRecord(field) && field.name === itemField)
    : undefined;
  const items = isRecord(declared) ? declared.items : undefined;
  const itemSchema = isRecord(items) ? items
    : isRecord(ref.item_schema) ? ref.item_schema : { type: 'string' };
  const properties = itemSchema.type === 'object' && isRecord(itemSchema.properties)
    ? itemSchema.properties : {};
  const projected = Object.entries(properties).flatMap(([name, value]) => (
    isRecord(value) ? [{ name, path: `$.${name}`, type: columnType(value) }] : []
  ));
  return {
    action_id: 'derive.table_from_list',
    scope: { kind: 'project' },
    sheet_name: req.sheet_name,
    params: {
      source: { kind: 'named_result', sheet_id: sheetId, column_id: columnId,
        run_id: ref.run_id, route, schema },
      item_schema: itemSchema as JsonValue,
      columns: projected.length ? projected
        : [{ name: 'value', path: '$', type: columnType(itemSchema) }],
    },
    output_names: {},
  };
}
