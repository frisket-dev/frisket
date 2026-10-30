import { useMemo } from 'react';
import type { CellValue, ReviewBundle, ReviewBundleField } from '../../api/types';
import { ReviewInputSources } from '../../components/review/ReviewInputSources';
import { ReviewSourcePreview } from '../../components/review/ReviewSourcePreview';
import type { ActionCompareSessionSnapshot } from '../actionCompareSession';

function cellValue(value: unknown): CellValue {
  if (value == null) return null;
  return typeof value === 'object' ? JSON.stringify(value) : value as CellValue;
}

/** Current inputs and real persisted evidence use Review's normal renderers. */
export function ComparisonSources({ state, rowId, field }: { state: ActionCompareSessionSnapshot; rowId: number; field: string }) {
  const { context } = state;
  const bundle = useMemo(() => {
    const basis = context.comparison_basis;
    const fields: ReviewBundleField[] = basis.kind === 'run' ? context.output_columns.flatMap((column) => {
      if (column.column_id == null) return [];
      const cell = context.baseline_rows?.[String(rowId)]?.[column.logical_key];
      return [{ id: `${column.column_id}:${rowId}`, runId: String(cell?.origin.run_id ?? basis.run_id),
        sheetId: String(context.sheet.id), rowId: String(rowId), columnId: String(column.column_id),
        columnName: column.current_name, outputKey: column.logical_key,
        columnType: column.column_type as ReviewBundleField['columnType'], format: column.format,
        semanticType: column.semantic_type, value: cellValue(cell?.value), confidence: null,
        justification: '', reviewState: 'unreviewed', role: 'field', chore: false, canEdit: false,
      }];
    }) : [];
    const sources = context.source_columns.map((column) => {
      const cell = context.source_rows[String(rowId)]?.[column.name];
      const bounded = cell?.bounded_value;
      return { columnId: String(column.column_id), columnName: column.name,
        columnType: column.column_type as ReviewBundleField['columnType'], semanticType: null, format: column.format,
        value: cellValue(cell?.value), ...(bounded ? { boundedValue: {
          preview: bounded.preview, totalCharacters: bounded.total_characters, totalBytes: bounded.total_bytes, downloadHref: null,
        } } : {}),
      };
    });
    const inputs = { sources, sheetId: String(context.sheet.id), rowId: String(rowId), rowIndex: context.sample_row_ids.indexOf(rowId) };
    if (basis.kind !== 'run' || !fields.length) return inputs;
    return { id: `comparison:${context.sheet.id}:${rowId}`, runId: String(basis.run_id), sheetId: String(context.sheet.id),
      sheetName: context.sheet.name, rowId: String(rowId), rowIndex: context.sample_row_ids.indexOf(rowId),
      actionKind: context.draft.action_id, actionName: context.draft.action_id, model: '', confidence: null,
      source: Object.fromEntries(sources.map((source) => [source.columnName, source.value])), sources,
      context: '', fields, evidence: [],
    } satisfies ReviewBundle;
  }, [context, rowId]);
  if (!('fields' in bundle)) return <ReviewInputSources bundle={bundle} />;
  return <ReviewSourcePreview bundle={bundle} activeField={bundle.fields.find((item) => item.outputKey === field)}
    sourceEntries={Object.entries(bundle.source)} />;
}
