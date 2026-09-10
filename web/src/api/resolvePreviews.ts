import { httpContract, type HttpContractSuccessResponse } from './httpContract';
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
} from './types';

export interface ResolvePreviewOptions {
  signal?: AbortSignal;
  headers?: HeadersInit;
}

export interface ClusterPreviewInput {
  sheetId: string;
  inputColumn: string;
  method: string;
  minSize?: number;
  threshold?: number;
  ngramSize?: number;
  keyTemplate?: string;
}

export interface ColumnValuesPreviewInput {
  sheetId: string;
  inputColumn: string;
  search?: string;
  limit?: number;
  offset?: number;
}

export interface ReplaceRulesPreviewInput {
  sheetId: string;
  inputColumn: string;
  rules: ResolveReplaceRuleDraft[];
  unmatched?: 'keep' | 'null';
  testValue?: string;
}

export type ClusterPreviewWire = HttpContractSuccessResponse<'tenant.cluster_preview.post'>;
export type ColumnValuesPreviewWire =
  HttpContractSuccessResponse<'tenant.column_values_preview.post'>;
export type ReplaceRulesPreviewWire =
  HttpContractSuccessResponse<'tenant.replace_rules_preview.post'>;

export interface ResolvePreviewsApi {
  cluster(
    projectId: string,
    input: ClusterPreviewInput,
    options?: ResolvePreviewOptions,
  ): Promise<ClusterPreviewResult>;
  columnValues(
    projectId: string,
    input: ColumnValuesPreviewInput,
    options?: ResolvePreviewOptions,
  ): Promise<ColumnValuesPreview>;
  replaceRules(
    projectId: string,
    input: ReplaceRulesPreviewInput,
    options?: ResolvePreviewOptions,
  ): Promise<ReplaceRulesPreview>;
}

type ContractErrorFactory = (status: number, payload: unknown) => Error;

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
    search: wire.search,
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
  errorFactory: ContractErrorFactory,
): ResolvePreviewsApi {
  return {
    cluster(projectId, input, options = {}) {
      return httpContract(
        'tenant.cluster_preview.post',
        {
          pathParams: { pid: projectId },
          query: {},
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
          headers: options.headers,
          errorFactory,
        },
        clusterResult,
      );
    },

    columnValues(projectId, input, options = {}) {
      return httpContract(
        'tenant.column_values_preview.post',
        {
          pathParams: { pid: projectId },
          query: {},
          body: {
            sheet_id: Number(input.sheetId),
            input_column: input.inputColumn,
            ...(input.search?.trim() ? { search: input.search } : {}),
            ...(input.limit != null ? { limit: input.limit } : {}),
            ...(input.offset != null ? { offset: input.offset } : {}),
          },
          signal: options.signal,
          headers: options.headers,
          errorFactory,
        },
        columnValuesResult,
      );
    },

    replaceRules(projectId, input, options = {}) {
      return httpContract(
        'tenant.replace_rules_preview.post',
        {
          pathParams: { pid: projectId },
          query: {},
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
          headers: options.headers,
          errorFactory,
        },
        replaceRulesResult,
      );
    },
  };
}
