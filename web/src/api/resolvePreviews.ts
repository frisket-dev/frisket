import { httpContract, type HttpContractSuccessResponse, type RequestOptions } from './httpContract';
import type {
  ClusterGroup,
  ClusterPreviewResult,
  ColumnListFacet,
  GridFilterListSelector,
  ColumnValueCount,
  ColumnValuesPreview,
  ReplaceRuleMatchCount,
  ReplaceRulesPreview,
  ResolveReplaceRuleDraft,
  ViewSelection,
} from './types';
import { toWireJsonObject } from './views';

interface ClusterPreviewInput {
  sheetId: string;
  inputColumn: string;
  method: string;
  minSize?: number;
  threshold?: number;
  ngramSize?: number;
  keyTemplate?: string;
}

interface ColumnValuesPreviewInput {
  sheetId: string;
  inputColumn: string;
  search?: string;
  limit?: number;
  offset?: number;
  valueMode?: 'exact' | 'bounded';
  /** Adds counts within the rows this selection keeps (`scope`). */
  selection?: ViewSelection | null;
}

interface ReplaceRulesPreviewInput {
  sheetId: string;
  inputColumn: string;
  rules: ResolveReplaceRuleDraft[];
  unmatched?: 'keep' | 'null';
  testValue?: string;
}

type ClusterPreviewWire = HttpContractSuccessResponse<'tenant.cluster_preview.post'>;
type ColumnValueCountsWire =
  HttpContractSuccessResponse<'tenant.column_value_counts.post'>;

/** One column's whole-column counts, as facet eligibility reads them. */
export interface ColumnValueCounts {
  inputColumn: string;
  distinct: number;
  omittedRows: number;
  listDistinct: number | null;
}

type ColumnValuesPreviewWire =
  HttpContractSuccessResponse<'tenant.column_values_preview.post'>;
type ReplaceRulesPreviewWire =
  HttpContractSuccessResponse<'tenant.replace_rules_preview.post'>;

/** Keep this narrow boundary defensive so an older server simply behaves as
 * one without a collection facet. */
type ListFacetWire = {
  distinct: number;
  offset: number;
  limit: number;
  truncated: boolean;
  search: string | null;
  choices: Array<{
    key: string;
    label: string;
    count: number;
    selector: unknown;
  }>;
};

function listSelector(value: unknown): GridFilterListSelector | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return null;
  const candidate = value as Record<string, unknown>;
  if (
    Object.keys(candidate).length === 2
    && candidate.kind === 'scalar'
    && (typeof candidate.value === 'string'
      || typeof candidate.value === 'boolean'
      || (typeof candidate.value === 'number' && Number.isFinite(candidate.value)))
  ) {
    return { kind: 'scalar', value: candidate.value };
  }
  if (
    Object.keys(candidate).length === 3
    && candidate.kind === 'entity'
    && typeof candidate.type === 'string'
    && candidate.type !== ''
    && typeof candidate.text === 'string'
    && candidate.text !== ''
  ) {
    return { kind: 'entity', type: candidate.type, text: candidate.text };
  }
  return null;
}

function listFacetResult(wire: ColumnValuesPreviewWire): ColumnListFacet | null {
  const raw = wire.list_facet;
  if (raw === undefined || raw === null || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const facet = raw as Partial<ListFacetWire>;
  if (
    typeof facet.distinct !== 'number'
    || !Number.isInteger(facet.distinct)
    || typeof facet.offset !== 'number'
    || !Number.isInteger(facet.offset)
    || typeof facet.limit !== 'number'
    || !Number.isInteger(facet.limit)
    || typeof facet.truncated !== 'boolean'
    || (facet.search !== null && typeof facet.search !== 'string')
    || !Array.isArray(facet.choices)
  ) return null;
  const choices = facet.choices.flatMap((choice) => {
    if (typeof choice !== 'object' || choice === null || Array.isArray(choice)) return [];
    const candidate = choice as Record<string, unknown>;
    const selector = listSelector(candidate.selector);
    if (
      !selector
      || typeof candidate.key !== 'string'
      || typeof candidate.label !== 'string'
      || typeof candidate.count !== 'number'
      || !Number.isInteger(candidate.count)
    ) return [];
    return [{
      key: candidate.key,
      label: candidate.label,
      count: candidate.count,
      selector,
    }];
  });
  // A malformed entry must not change the meaning of an otherwise valid
  // server-authored facet. Refuse that facet rather than displaying a partial
  // menu whose selections users cannot account for.
  if (choices.length !== facet.choices.length) return null;
  return {
    distinct: facet.distinct,
    offset: facet.offset,
    limit: facet.limit,
    truncated: facet.truncated,
    search: facet.search,
    choices,
  };
}

function clusterResult(wire: ClusterPreviewWire): ClusterPreviewResult {
  return {
    clusters: wire.clusters.map((cluster): ClusterGroup => ({
      key: cluster.key,
      canonical: cluster.canonical,
      size: cluster.size,
      values: cluster.values.map((value) => ({ value: value.value, count: value.count })),
      rowIds: cluster.row_ids.map(String),
    })),
    count: wire.count,
    valueHash: wire.value_hash,
    method: wire.method,
    semantic: wire.semantic,
  };
}

function columnValuesResult(wire: ColumnValuesPreviewWire): ColumnValuesPreview {
  const listFacet = listFacetResult(wire);
  return {
    sheetId: String(wire.sheet_id),
    columnId: String(wire.column_id),
    inputColumn: wire.input_column,
    totalRows: wire.total_rows,
    distinct: wire.distinct,
    missing: wire.missing,
    values: wire.values.map((value): ColumnValueCount => ({
      value: value.value,
      count: value.count,
    })),
    offset: wire.offset,
    limit: wire.limit,
    truncated: wire.truncated,
    valueHash: wire.value_hash,
    ...(typeof wire.inventory_complete === 'boolean' ? { inventoryComplete: wire.inventory_complete } : {}),
    ...(typeof wire.omitted_rows === 'number' ? { omittedRows: wire.omitted_rows } : {}),
    search: wire.search,
    ...(wire.scope ? {
      scope: {
        totalRows: wire.scope.total_rows,
        values: wire.scope.values.map((value): ColumnValueCount => ({ value: value.value, count: value.count })),
        distributionCounts: wire.scope.distribution_counts,
      },
    } : {}),
    // Keep the browser shape backwards-compatible with servers that predate
    // this additive wire field. A supplied but invalid facet remains explicit
    // as null, so consumers never render a partial set of choices.
    ...(Object.prototype.hasOwnProperty.call(wire, 'list_facet') ? { listFacet } : {}),
    distribution: wire.distribution?.kind === 'number'
      ? {
          kind: 'number',
          min: wire.distribution.min,
          max: wire.distribution.max,
          bins: wire.distribution.bins.map((bin) => ({
            start: bin.start,
            end: bin.end,
            count: bin.count,
          })),
        }
      : wire.distribution?.kind === 'integer'
        ? {
            kind: 'integer',
            min: wire.distribution.min,
            max: wire.distribution.max,
            bins: wire.distribution.bins.map((bin) => ({
              start: bin.start,
              end: bin.end,
              count: bin.count,
            })),
          }
      : wire.distribution?.kind === 'date'
        ? {
            kind: 'date',
            min: wire.distribution.min,
            max: wire.distribution.max,
            bins: wire.distribution.bins.map((bin) => ({
              start: bin.start,
              end: bin.end,
              count: bin.count,
            })),
          }
        : null,
  };
}

function replaceRulesResult(wire: ReplaceRulesPreviewWire): ReplaceRulesPreview {
  const testResult = wire.test_result === null
    ? null
    : {
        matchedRuleIndex: wire.test_result.matched_rule_index === null
          ? null
          : wire.test_result.matched_rule_index,
        output: wire.test_result.output,
      };
  return {
    sheetId: String(wire.sheet_id),
    columnId: String(wire.column_id),
    totalRows: wire.total_rows,
    ruleCounts: wire.rule_counts.map((rule): ReplaceRuleMatchCount => ({
      index: rule.index,
      matchedRows: rule.matched_rows,
      matchedValues: rule.matched_values,
    })),
    unmatchedRows: wire.unmatched_rows,
    testResult,
    valueHash: wire.value_hash,
  };
}

export function createResolvePreviewsApi(
  projectId: string,
) {
  return {
    async clusterPreview(
      input: ClusterPreviewInput,
      options: RequestOptions = {},
    ): Promise<ClusterPreviewResult> {
      return httpContract(
        'tenant.cluster_preview.post',
        {
          pathParams: { pid: projectId },
          body: {
            sheet_id: Number(input.sheetId),
            input_column: input.inputColumn,
            method: input.method,
            min_size: input.minSize ?? 2,
            ...(input.threshold != null ? { threshold: input.threshold } : {}),
            ...(input.ngramSize != null ? { ngram_size: input.ngramSize } : {}),
            ...(input.keyTemplate?.trim() ? { key_template: input.keyTemplate.trim() } : {}),
          },
          signal: options.signal,
        },
        clusterResult,
      );
    },

    /** Read-only distinct-value inventory for the RESOLVE authoring surfaces
     *  (POST /column-values/v1/preview). `search` is a case-insensitive
     *  substring filter over values; limit defaults server-side to 500 (max
     *  2000), offset to 0. */
    async columnValuesPreview(
      input: ColumnValuesPreviewInput,
      options: RequestOptions = {},
    ): Promise<ColumnValuesPreview> {
      return httpContract(
        'tenant.column_values_preview.post',
        {
          pathParams: { pid: projectId },
          body: {
            sheet_id: Number(input.sheetId),
            input_column: input.inputColumn,
            ...(input.search?.trim() ? { search: input.search } : {}),
            ...(input.limit != null ? { limit: input.limit } : {}),
            ...(input.offset != null ? { offset: input.offset } : {}),
            ...(input.valueMode != null ? { value_mode: input.valueMode } : {}),
            ...(input.selection ? { selection: toWireJsonObject(input.selection) } : {}),
          },
          signal: options.signal,
        },
        columnValuesResult,
      );
    },

    /** Whole-column distinct counts for several columns in one request
     *  (POST /column-values/v1/counts). */
    async columnValueCounts(
      input: { sheetId: string; inputColumns: string[] },
      options: RequestOptions = {},
    ): Promise<{ columns: ColumnValueCounts[] }> {
      return httpContract(
        'tenant.column_value_counts.post',
        {
          pathParams: { pid: projectId },
          body: { sheet_id: Number(input.sheetId), input_columns: input.inputColumns },
          signal: options.signal,
        },
        (wire: ColumnValueCountsWire) => ({
          columns: wire.columns.map((column) => ({
            inputColumn: column.input_column,
            distinct: column.distinct,
            omittedRows: column.omitted_rows,
            listDistinct: column.list_distinct,
          })),
        }),
      );
    },

    /** Read-only rules dry-run for the Replace drawer (POST
     *  /replace-rules/v1/preview): per-rule first-match-wins counts plus an
     *  optional single-value live test. */
    async replaceRulesPreview(
      input: ReplaceRulesPreviewInput,
      options: RequestOptions = {},
    ): Promise<ReplaceRulesPreview> {
      return httpContract(
        'tenant.replace_rules_preview.post',
        {
          pathParams: { pid: projectId },
          body: {
            sheet_id: Number(input.sheetId),
            input_column: input.inputColumn,
            rules: input.rules.map((rule) => ({
              match: rule.match,
              pattern: rule.pattern,
              target: rule.target,
              case_sensitive: rule.case_sensitive === true,
            })),
            ...(input.unmatched ? { unmatched: input.unmatched } : {}),
            ...(input.testValue != null ? { test_value: input.testValue } : {}),
          },
          signal: options.signal,
        },
        replaceRulesResult,
      );
    },
  };
}
